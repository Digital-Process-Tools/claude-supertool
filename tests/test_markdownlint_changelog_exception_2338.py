"""markdownlint\'s generic ruleset has no exception for changelog.d\'s own
convention -- bullet-only, no heading, no wrap -- so every fragment a lane
writes in a project using that convention gets flagged for a shape the
project deliberately chose (#2338).

Filed against `Digital-Process-Tools/claude-oss`, not this repo: this repo\'s
own `.markdownlint.json` already disables MD013/MD041 globally, which is why
the noise never showed up in `test_markdownlint_noise_2012.py`\'s sweep -- a
*consuming* project with no such override (the ordinary case; this repo\'s
own config is the exception, not the rule) gets markdownlint\'s stock
ruleset, unmodified, on every `changelog.d/*.md` write.

The fix is generic rather than hardcoded to "no heading, no wrap": a fragment
matching the same `*changelog.d/*.md` glob `.supertool.json` already wires
`changelog-fragment` to is `skipped` here -- a scope decline, the same shape
`stylelint`\'s `AllFilesIgnoredError` case uses (`docs/validators.md`) -- but
ONLY when the project has actually adopted the convention, i.e. its own
changelog assembler script (`.github/scripts/assemble_changelog.py`,
`.oss/assemble_changelog.py` or `scripts/assemble_changelog.py` -- the same
three locations `changelog-fragment.py`\'s `ASSEMBLER_LOCATIONS` tries, in the
same order) is discoverable above the file, bounded at the repo root. A
project with a `changelog.d/` directory that never adopted this convention
still gets ordinary markdownlint coverage there.

Three cases, and the second and third are positive controls: without them,
"the fragment passed" would be indistinguishable from "nothing ran" or "every
changelog.d/ path is silently exempted regardless of convention".
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "validators" / "markdownlint" / "markdownlint.py"

# Deliberately no heading and well past 80 columns -- this repo\'s own
# changelog.d/ convention (docs/CLAUDE.md, changelog.d/README.md), and what
# every real fragment under changelog.d/ here looks like.
FRAGMENT_BODY = (
    "- **Fixed a thing that was quite badly broken for a genuinely long "
    "time and needed a fix** (#100). Longer explanation here that goes "
    "well beyond eighty characters on purpose to trip MD013.\n"
)


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)


def _run(file_path: Path, cwd: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(file_path)],
        capture_output=True, text=True, cwd=str(cwd),
        encoding="utf-8", errors="replace",
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(not shutil.which("markdownlint"), reason="markdownlint not on PATH")
def test_changelog_fragment_passes_when_project_owns_the_convention(tmp_path: Path) -> None:
    """A changelog.d/*.md fragment in a project with its own assembler script
    is deferred to `changelog-fragment`, not flagged by markdownlint\'s
    generic ruleset, even with no local `.markdownlint.json` override."""
    _git_init(tmp_path)
    (tmp_path / ".oss").mkdir()
    (tmp_path / ".oss" / "assemble_changelog.py").write_text("# stub\n")
    frag_dir = tmp_path / "changelog.d"
    frag_dir.mkdir()
    frag = frag_dir / "100.fixed.md"
    frag.write_text(FRAGMENT_BODY)

    out = _run(frag, tmp_path)

    assert "skipped" in out, out
    assert "ok" not in out, out
    assert "changelog-fragment" in out["skipped"], out


@pytest.mark.skipif(not shutil.which("markdownlint"), reason="markdownlint not on PATH")
def test_ordinary_md_file_with_the_same_shape_is_still_flagged(tmp_path: Path) -> None:
    """Positive control: the exception is scoped to changelog.d/*.md paths in
    a project that owns the convention, never a blanket relaxation of
    MD013/MD041 -- an ordinary file outside changelog.d/ with the identical
    no-heading, long-line shape still trips the generic ruleset."""
    _git_init(tmp_path)
    (tmp_path / ".oss").mkdir()
    (tmp_path / ".oss" / "assemble_changelog.py").write_text("# stub\n")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    plain = docs_dir / "plain.md"
    plain.write_text(FRAGMENT_BODY)

    out = _run(plain, tmp_path)

    assert out.get("ok") is False, out
    codes = {e["code"] for e in out["errors"]}
    assert "MD013/line-length" in codes or "MD041/first-line-heading/first-line-h1" in codes, out


@pytest.mark.skipif(not shutil.which("markdownlint"), reason="markdownlint not on PATH")
def test_changelog_fragment_not_exempted_when_project_has_no_assembler(tmp_path: Path) -> None:
    """A `changelog.d/*.md` path in a project that never adopted the
    convention (no assembler script anywhere above it) is not silently
    exempted -- the glob alone is not enough, or a project with an unrelated
    `changelog.d/` directory would go unchecked."""
    _git_init(tmp_path)
    frag_dir = tmp_path / "changelog.d"
    frag_dir.mkdir()
    frag = frag_dir / "100.fixed.md"
    frag.write_text(FRAGMENT_BODY)

    out = _run(frag, tmp_path)

    assert out.get("ok") is False, out


def test_repo_root_walk_does_not_climb_past_a_symlinked_boundary() -> None:
    """Self-review finding: `git rev-parse --show-toplevel` resolves symlinks
    (chdir + getcwd), so comparing it against a plain `os.path.abspath`
    walk never matches on a tree reached through a symlink -- macOS's own
    default `TMPDIR` is one (`/var` -> `/private/var`) -- and the walk then
    climbs past the true repo root into an unrelated ancestor directory,
    silently picking up ITS assembler script as though it belonged to this
    project.

    Deliberately NOT using pytest's `tmp_path` fixture: it is already
    resolved to the physical path (observed: `/private/var/folders/...` on
    this machine), which would erase the exact logical/physical mismatch
    this test exists to exercise. `tempfile.mkdtemp()` returns the RAW,
    unresolved `TMPDIR`-relative path (`/var/folders/...`), which is the
    shape that actually reproduces this on a stock macOS box -- confirmed by
    reverting the fix locally and watching this test go red before writing
    it here, exactly what a tmp_path-based version failed to do.

    Reproduced directly against `_owned_by_changelog_fragment` rather than
    through the CLI, since the walk is the thing at issue and the CLI adds
    nothing to the assertion; no `markdownlint` binary needed."""
    import importlib.util
    import shutil as _shutil
    import tempfile

    raw_root = tempfile.mkdtemp()
    try:
        outer = os.path.join(raw_root, "outer")
        os.makedirs(os.path.join(outer, ".oss"))
        with open(os.path.join(outer, ".oss", "assemble_changelog.py"), "w") as f:
            f.write("# outer stub -- must NOT be found\n")
        repo = os.path.join(outer, "repo")
        os.makedirs(repo)
        _git_init(Path(repo))
        frag_dir = os.path.join(repo, "changelog.d")
        os.makedirs(frag_dir)
        frag = os.path.join(frag_dir, "100.fixed.md")
        with open(frag, "w") as f:
            f.write(FRAGMENT_BODY)

        spec = importlib.util.spec_from_file_location(
            "_st_markdownlint_2338", str(ADAPTER))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod._owned_by_changelog_fragment(frag) is False, (
            "the walk climbed past the repo root and picked up "
            "outer/.oss/assemble_changelog.py, which belongs to no project "
            "this fragment is part of"
        )
    finally:
        _shutil.rmtree(raw_root, ignore_errors=True)


def test_constants_do_not_drift_from_their_sources_of_truth() -> None:
    """Self-review (auditor finding): `CHANGELOG_FRAGMENT_GLOB_DEFAULT` and
    `CHANGELOG_ASSEMBLER_LOCATIONS` in markdownlint.py are fresh literals
    claimed in a comment to equal `.supertool.json`'s `changelog-fragment`
    entry and `changelog-fragment.py`'s own `ASSEMBLER_LOCATIONS`, "so the
    two never drift" -- a claim nothing enforced. This is the same shape
    `ci_lint_resolve_root.py`'s `RESOLVE_ERROR_PREFIX` comment names and
    `test_resolve_error_prefix_pinned_2229.py` guards: a second,
    independently-typed literal goes red here the moment either source
    changes without this one following."""
    import importlib.util

    repo_root = Path(__file__).parent.parent

    md_spec = importlib.util.spec_from_file_location(
        "_st_markdownlint_2338_pin",
        str(repo_root / "validators" / "markdownlint" / "markdownlint.py"))
    md = importlib.util.module_from_spec(md_spec)
    md_spec.loader.exec_module(md)

    cf_spec = importlib.util.spec_from_file_location(
        "_st_changelog_fragment_2338_pin",
        str(repo_root / "validators" / "changelog-fragment" / "changelog-fragment.py"))
    cf = importlib.util.module_from_spec(cf_spec)
    cf_spec.loader.exec_module(cf)

    assert md.CHANGELOG_ASSEMBLER_LOCATIONS == cf.ASSEMBLER_LOCATIONS, (
        "markdownlint.py's assembler search order has drifted from "
        "changelog-fragment.py's ASSEMBLER_LOCATIONS -- update the mirrored "
        "constant"
    )

    cfg = json.loads((repo_root / ".supertool.json").read_text(encoding="utf-8"))
    configured_glob = cfg["validators"]["changelog-fragment"]["match"]
    assert md.CHANGELOG_FRAGMENT_GLOB_DEFAULT == configured_glob, (
        "markdownlint.py's default changelog-fragment glob has drifted "
        "from .supertool.json's own changelog-fragment.match"
    )
