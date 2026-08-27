"""markdownlint fired on 846 findings across 50 of 82 tracked `.md` files,
almost all of it genre noise rather than a real defect (#2012).

Measured at 5f0c4cb through this exact adapter (`validators/markdownlint/
markdownlint.py`, invoked per tracked `.md` file, same as this module's own
`_lint_all_tracked_md` helper): 846 findings, 50 files. The top offenders and
why each one is disabled here rather than fixed file-by-file:

- **MD040** (327 hits, 39% of the total) -- fenced code blocks with no
  language. This repo's prose fences op output, receipts and refusal text;
  there is no correct language tag for a pasted `gh-branch` render.
- **MD038** (143) -- spaces inside code spans. Every hit found while fixing
  this issue was a deliberate quote of a literal space, e.g. `` `  --  ` ``
  in docs/contributing.md.
- **MD033** (37) -- inline HTML. Two real uses: README.md's banner image
  (`<p align="center"><img ...></p>`, which plain Markdown cannot center or
  size) and angle-bracket CLI placeholder notation (`<file>`, `<remote>`)
  quoted inside table cells and code spans, which is not HTML at all.
- **MD056** (21) -- table column count. Every instance found was a literal
  `|` documenting an op's own optional-argument syntax
  (`bluesky_publish:...[|REPLY_TO_AT_URI[|force]]`) inside a backtick code
  span. GFM does not treat a pipe inside a code span as a column delimiter;
  markdownlint's table parser is line-based and does not know that, so it
  miscounts columns that render correctly on GitHub. MD052 (1 hit, the same
  `[...]` optional-argument notation misread as a reference-link) is the
  same parser limitation and disabled for the same reason.
- **MD024** (123) -- duplicate heading text. 122 of those are in
  CHANGELOG.md, where "### Added" / "### Fixed" / "### Changed" repeat once
  per release by the Keep a Changelog format itself -- not a mistake. (One
  `siblings_only` run during this fix surfaced a real oddity -- two
  "### Added" blocks under one `## [0.25.0]` release with no separator
  between them -- which looks like a genuine `assemble_changelog.py` fold
  defect and was reported rather than hand-patched into a generated file;
  see the PR body for #2012.)
- **MD025** (37) -- multiple top-level headings. All 37 are in
  `.claude/jit-context/**` rule bodies, which use repeated `#` headings as
  section markers under one frontmatter block by the JIT vocabulary format,
  not as separate documents.
- **MD014** (2) -- `$ command` shown without output. Both hits in
  docs/presets/github.md show the result as a trailing `#`-comment on the
  same line (`$ gh run list ... # 2 runs exit 0`) rather than on a
  following line; the rule's heuristic does not recognise that shape as
  "showing output".

**MD031, MD022 and MD018 stay on and were fixed at the source instead**,
per the issue's own instruction not to trade signal for a quiet validator:
blank lines were inserted around fences/lists/headings (MD031, MD022,
MD032, MD012, MD004, MD049, MD009, MD034 -- all auto-fixable, verified with
`markdownlint --fix` and reviewed by diff before applying), and the 8 MD018
hits were all the same false-positive shape -- a hard-wrapped prose line
that happens to start with an issue reference (`#1426, #1430 and #1433`) --
fixed by escaping the leading hash (`\\#1426`) rather than disabling the
rule, so it still catches an actual malformed ATX heading. `--fix` was
tried on MD018 directly first and rejected: it inserts a space after the
hash, which turns the false-positive line into a real heading and cascades
into MD022/MD025/MD001 -- a worse document, not a linted one.

**Residue, not fixed here:**

- CHANGELOG.md still carries 21 findings (MD022/MD032/MD049/MD012, all
  genuine per the rules above) after every other change in this file.
  It is a generated, historical record assembled from `changelog.d/`
  fragments by `assemble_changelog.py`; hand-editing already-released
  entries is against this repo's own convention ("never edit CHANGELOG.md
  in a PR") and out of this issue's scope, which is the live-editing
  experience, not the archive.
- `.claude/jit-context/paths/00-manual/changelog-d.md` carries 5 findings
  (MD018/MD022/MD023/MD031) from one nested-fence-inside-a-fence example
  block illustrating fence-inside-a-bullet syntax. markdownlint's
  line-based parser cannot see inside the outer fence, so it reads the
  inner fence's own content (` ## Quoted Heading `) as real headings.
  Documenting fence-in-fence syntax needs an example that contains one;
  there is no way to write this example that both renders correctly and
  reads clean to a linter that does not understand nested fences.

Post-fix: 26 findings across 2 of 84 tracked `.md` files (both named
above), down from 846 across 50 of 82.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "markdownlint" / "markdownlint.py"

# Rules disabled by #2012, beyond the three #1975 already turned off.
NEWLY_DISABLED = [
    "MD040", "MD038", "MD033", "MD056", "MD052", "MD024", "MD025", "MD014",
]

# The rules the #2012 issue explicitly said must stay on -- genuine
# formatting defects, fixed at the source rather than silenced.
MUST_STAY_ON = ["MD031", "MD022", "MD018"]

# A ceiling, not the exact count: this repo's tracked `.md` files change
# over time, and a new file with a genuine, unrelated formatting slip
# should fail *that* file's own lint, not this test. 40 gives headroom
# above the 26 measured at fix time without being wide enough to hide a
# regression back toward hundreds.
FINDINGS_CEILING = 40

# Files this issue's own fix leaves with residual findings, and why --
# see the module docstring. A tracked `.md` file NOT in this set that
# still has findings is what this test exists to catch.
KNOWN_RESIDUE = {
    "CHANGELOG.md",
    ".claude/jit-context/paths/00-manual/changelog-d.md",
}


def _lint_all_tracked_md() -> dict[str, int]:
    """Run the shipped adapter directly against every tracked `.md` file,
    the same way the issue's own measurement loop did. Returns {path: count}
    for every file with at least one finding; a `skipped` verdict (no
    markdownlint on PATH) is excluded rather than counted as zero, so a
    missing tool cannot masquerade as a clean repo."""
    files = subprocess.run(
        ["git", "ls-files", "*.md"], capture_output=True, text=True, cwd=REPO
    ).stdout.splitlines()
    per_file: dict[str, int] = {}
    for f in files:
        result = subprocess.run(
            [sys.executable, str(ADAPTER), f],
            capture_output=True, text=True, cwd=REPO,
        )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if data.get("skipped"):
            continue
        count = data.get("count", 0)
        if count:
            per_file[f] = count
    return per_file


def test_markdownlint_config_disables_the_genre_rules_measured_in_2012() -> None:
    cfg = json.loads((REPO / ".markdownlint.json").read_text(encoding="utf-8"))
    for rule in NEWLY_DISABLED:
        assert cfg.get(rule) is False, (
            f"{rule} must be disabled -- see this module's docstring for "
            "the measured genre mismatch #2012 found for it"
        )


def test_markdownlint_config_keeps_the_rules_2012_said_must_stay_on() -> None:
    cfg = json.loads((REPO / ".markdownlint.json").read_text(encoding="utf-8"))
    for rule in MUST_STAY_ON:
        assert rule not in cfg or cfg[rule] is not False, (
            f"{rule} must stay enabled -- #2012 fixed its findings at the "
            "source rather than silencing it, and disabling it here would "
            "throw that signal away"
        )


def test_repo_wide_markdownlint_findings_stay_under_the_2012_ceiling() -> None:
    """The acceptance test #2012 itself names: run the same loop that
    measured 846 and require the total to be something a reader will
    actually read. Skips (no markdownlint on PATH) rather than passing
    vacuously: see the positive control below."""
    per_file = _lint_all_tracked_md()
    total = sum(per_file.values())
    if total == 0 and not per_file:
        # Could be a genuinely clean repo, or markdownlint absent from
        # PATH and every file reporting `skipped`. Tell them apart before
        # trusting the zero.
        probe = subprocess.run(
            [sys.executable, str(ADAPTER), "README.md"],
            capture_output=True, text=True, cwd=REPO,
        )
        probe_data = json.loads(probe.stdout)
        if probe_data.get("skipped"):
            import pytest
            pytest.skip(
                "markdownlint not on PATH -- cannot verify the findings "
                "ceiling, see " + str(probe_data.get("skipped"))
            )
    assert total <= FINDINGS_CEILING, (
        f"repo-wide markdownlint findings rose to {total} across "
        f"{len(per_file)} files (ceiling {FINDINGS_CEILING}) -- "
        f"{sorted(per_file.items(), key=lambda kv: -kv[1])}"
    )


def test_markdownlint_positive_control_a_real_finding_is_still_caught() -> None:
    """Negative assertions (`findings stay low`) pass on a harness that
    silently checked nothing. Prove the loop still sees a real defect by
    linting a scratch file with an unambiguous, always-on violation
    (MD012, multiple consecutive blank lines) that no rule disabled above
    touches."""
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", dir=REPO, delete=False
    ) as fh:
        fh.write("# Scratch\n\n\n\ntext\n")
        scratch = Path(fh.name)
    try:
        result = subprocess.run(
            [sys.executable, str(ADAPTER), scratch.name],
            capture_output=True, text=True, cwd=REPO,
        )
        data = json.loads(result.stdout)
        assert data.get("count", 0) >= 1, (
            "a file with an obvious MD012 violation reported zero findings "
            f"-- the harness cannot see anything: {data}"
        )
    finally:
        scratch.unlink(missing_ok=True)


def test_known_residue_files_are_the_only_ones_left_with_findings() -> None:
    """Pins the residue named in the module docstring to specific files,
    so a *new* file joining the noisy set is a red here rather than
    quietly padding out the ceiling above."""
    per_file = _lint_all_tracked_md()
    unexpected = set(per_file) - KNOWN_RESIDUE
    assert not unexpected, (
        "a tracked .md file outside the documented #2012 residue now has "
        f"markdownlint findings: {sorted(unexpected)} -- either fix it or "
        "add it to KNOWN_RESIDUE with a reason in this module's docstring"
    )
