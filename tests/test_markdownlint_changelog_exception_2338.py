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
