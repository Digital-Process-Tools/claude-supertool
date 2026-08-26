"""markdownlint is a third dead validator: registered in the example config,
matching 82 real `.md` files here, and wired into this repo nowhere (#1975).

Same class as #1795 (`yaml-check`, `actionlint`): an adapter can be present,
tested, and reachable from `.supertool.example.json` -- the template users
copy, not what runs here -- while this repo's own `.supertool.json` never
mentions it, so it has never fired on a Markdown edit in this checkout.
`test_every_adapter_is_registered_in_at_least_one_config` in
tests/test_validator_registration_reachability_1795.py does not catch this
shape: markdownlint IS reachable from the example config, so that generic
sweep stayed green the whole time this gap was live. This file pins the local
registration by name, the same way #1974 pinned yaml-check/actionlint. (The
issue text cites 82 matching `.md` files at filing time; this repo's tracked
count moves as files are added and removed, so no test here asserts a
specific figure -- what is pinned is that the glob matches at all.)

The posture decision this issue asks for: `rollback_on_fail: false`, not
`true`. markdownlint is a *style* linter over a large body of prose-heavy
Markdown here (`CLAUDE.md`, `docs/`, every `changelog.d/` fragment, every
`.claude/jit-context/` entry) -- a line-length or heading-style opinion
reverting a correct edit would be worse than the finding just being advisory.
Measured against this repo's own real files before choosing: markdownlint's
default rule set flags MD013 (line-length) on nearly every prose paragraph
here (47 hits on CLAUDE.md alone) and MD060 (table-column-style) on every
pipe-table row -- both style opinions this repo's own house style already
rejects (long lines are the deliberate choice CLAUDE.md's own prose makes).
`.markdownlint.json` at the repo root disables both; the remaining default
rules (unlabeled fenced code blocks, stray table columns, multiple blank
lines, spaces inside code spans, ...) are left on because they caught real,
specific problems when run against this repo's own docs/validators.md.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool

REPO = Path(__file__).resolve().parent.parent


def test_markdownlint_fires_on_a_md_edit_in_this_repo(shipped_config) -> None:
    applicable = supertool._applicable_validators("edit", "docs/validators.md")
    assert "markdownlint" in applicable, (
        "markdownlint ships, is tested, and must fire on a .md edit in this "
        f"repo's own config -- got {sorted(applicable)}"
    )


def test_markdownlint_does_not_fire_on_a_non_markdown_edit(shipped_config) -> None:
    """Positive control for the match scope: a plain .py edit is not Markdown,
    and markdownlint's own match (`*.md`) says so -- without this, the "must
    fire" assertion above would be meaningless if the glob fired on
    everything."""
    applicable = supertool._applicable_validators("edit", "supertool.py")
    assert "markdownlint" not in applicable, (
        f"markdownlint fired on a non-.md file -- got {sorted(applicable)}"
    )


def test_markdownlint_registration_does_not_roll_back_on_a_finding() -> None:
    """The posture decision this issue names explicitly: a style finding must
    not revert a correct edit. Read directly from the shipped config rather
    than asserted in prose, so a later edit that flips this to `true` fails
    here rather than silently changing behaviour on this repo's own very
    large body of prose."""
    cfg = json.loads((REPO / ".supertool.json").read_text(encoding="utf-8"))
    spec = cfg["validators"]["markdownlint"]
    assert spec["rollback_on_fail"] is False, (
        "markdownlint is a style linter over prose-heavy docs here -- "
        "rollback_on_fail must stay False, or a line-length opinion would "
        "revert a correct edit"
    )


def test_markdownlint_config_disables_the_rules_that_misfire_on_this_repo() -> None:
    """`.markdownlint.json` exists and turns off exactly the rules measured
    to misfire against real files here, rather than leaving markdownlint at
    its noisy default -- see the module docstring for the measurements:

    - MD013 (line-length): 47 hits on CLAUDE.md alone -- this repo's own
      house style is long prose paragraphs, not 80-column wrapping.
    - MD060 (table-column-style): fires on every pipe-table row.
    - MD041 (first-line-heading): fires on README.md itself (it opens with
      an HTML banner image, a deliberate choice, not a heading) and on
      every changelog.d/*.md fragment (a bullet destined for CHANGELOG.md,
      never meant to stand alone as a headed document) -- measured on the
      changelog fragment this issue's own pull request adds, the very
      first write to reach this validator once it was registered locally.
      (Not named by its on-disk path here: a pending changelog.d fragment
      is deleted by the release that ships it, and a reference keyed to
      that path is green only until then -- see
      tests/_changelog_findable.py.)

    A `.markdownlintignore` scoped to changelog.d/ was tried first and
    reverted: markdownlint-cli's `ignore` dependency raises a `RangeError`
    on any target file it cannot express relative to cwd without a `../`
    traversal -- which is every file under a pytest tmp_path, i.e. this
    repo's own test suite. An ignore file that crashes the tool on any
    file outside the repo root is worse than the noise it was meant to
    silence; disabling MD041 globally has no such failure mode.
    """
    cfg_path = REPO / ".markdownlint.json"
    assert cfg_path.is_file(), (
        ".markdownlint.json must exist -- markdownlint's own default rule "
        "set is not usable against this repo without it"
    )
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("MD013") is False, "MD013 (line-length) must be disabled for this repo's prose"
    assert cfg.get("MD060") is False, "MD060 (table-column-style) must be disabled"
    assert cfg.get("MD041") is False, (
        "MD041 (first-line-heading) must be disabled -- it misfires on README.md's "
        "own banner image and on every changelog.d/*.md fragment"
    )


def test_no_markdownlintignore_file_is_shipped() -> None:
    """Positive control for the reverted approach above: an ignore file must
    not reappear, because it is what caused the crash this test's sibling
    documents -- markdownlint-cli's `ignore` dependency raising on any
    target path outside the repo root (every pytest tmp_path fixture)."""
    assert not (REPO / ".markdownlintignore").exists(), (
        ".markdownlintignore must not exist -- see "
        "test_markdownlint_config_disables_the_rules_that_misfire_on_this_repo "
        "for why (markdownlint-cli's ignore dependency crashes on any target "
        "path outside the repo root when an ignore file is present)"
    )
