"""The version sites are checked against a list, not one test per site (#1854).

Four separate tests already pin four of the five sites to `supertool.VERSION`
(`test_mcp_config_279.py` for `.claude-plugin/plugin.json` and the README badge,
`test_pyproject_version_522.py`, `test_changelog_link_refs_918.py`). #1854 was
filed on the belief that the README badge was guarded by nothing; that was true
when the badge sat fifteen releases stale and is **not true now** --
`test_readme_version_badge_matches_code` has guarded it since the drift was
found. `CLAUDE.md` still said "only four are guarded" and is corrected in this
same commit.

So what is missing is not a guard on a site. It is a guard on the **set**.

Five hand-written tests in four files cover five hand-written paths. Nothing
connects them to the repository's own declared list of version sites, so a
**sixth** site gains no guard by being declared, and the failure is silent in
the direction that matters: every existing test still passes, and the new site
drifts exactly the way the badge did. `CLAUDE.md` names that as the sweep's real
value -- "finding a sixth site no test covers" -- and left it to a human running
`git grep` at release time.

**The list is `.oss.json`'s `version_sites`, and that is a decision.** It is the
only machine-readable statement of the set that already exists; before this file
it was read by nothing in this repository, only by the external maintainer loop
that consumes the config. A list written *here* would be what the issue rules
out -- a sixth place the version lives. `CLAUDE.md`'s prose copy is now a
pointer to this file rather than a second list.

**What each state means, because the interesting ones are the absences:**

- a declared site with no extractor is a **failure**, not a skip. That is the
  whole mechanism: declaring a sixth site turns this file red until somebody
  says how to read a version out of it.
- a declared site whose extractor matches nothing is a **failure**, not
  agreement. Zero matches has not cleared the site, it has failed to look at it
  -- the same three-states rule `test_readme_version_badge_matches_code` states.
- an empty site list is a **failure**. A test that iterates nothing passes, and
  a passing empty loop is indistinguishable from five agreeing sites.

**The control is the point of the file.** `test_a_frozen_site_is_caught` builds
a tree where four sites agree and the fifth is frozen at an older version, and
asserts the checker reports it. Without that, a checker that reads four sites
and never reaches the fifth passes exactly as the repository did while the badge
was stale -- which is the state this issue is about.

**Where the refusal lands.** The issue asks whether the release flow should
refuse a tag on disagreement rather than report it. In this repository it now
does, without any new gate: this file is in the suite, the suite runs on every
pull request, and `.githooks/pre-push` runs it again for a push whose
destination is `master`. Direct pushes to `master` are the path release commits
actually take -- 13 of the last 400 commits, per #1854's own table -- and that
arm is the one gate they pass. A second refusal inside the external release flow
would be a change to another repository and is left to it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import supertool

ROOT = Path(supertool.__file__).resolve().parent

#: How to read the declared version out of each site. Keys are paths as they
#: appear in `.oss.json`'s `version_sites`; this maps path -> reader, and holds
#: no version of its own, so it is not a sixth place the version lives.
#:
#: Each reader returns the version string it found, or None for "I could not
#: find one" -- never a default, because a default is the absence this
#: repository keeps filing.


def _from_plugin_json(text: str):
    try:
        return json.loads(text).get("version")
    except json.JSONDecodeError:
        return None


def _first_group(pattern: str):
    compiled = re.compile(pattern, re.MULTILINE)

    def read(text: str):
        match = compiled.search(text)
        return match.group(1) if match else None

    return read


READERS = {
    ".claude-plugin/plugin.json": _from_plugin_json,
    "_supertool.py": _first_group(r'^VERSION\s*=\s*"([^"]+)"'),
    "pyproject.toml": _first_group(r'^version\s*=\s*"([^"]+)"'),
    # The newest release heading. `[Unreleased]` carries no version and is
    # skipped by the digit class rather than by position.
    "CHANGELOG.md": _first_group(r"^## \[(\d+\.\d+\.\d+)\]"),
    "README.md": _first_group(
        r"!\[Version\]\(https://img\.shields\.io/badge/version-(\d+\.\d+\.\d+)-"),
}


def declared_sites(root: Path = ROOT):
    """The version sites, read from the repository's own declared list."""
    config = json.loads((root / ".oss.json").read_text(encoding="utf-8"))
    return list(config.get("version_sites", []))


def read_site(root: Path, rel: str):
    """(version, problem). Exactly one of the two is None."""
    reader = READERS.get(rel)
    if reader is None:
        return None, (
            f"`{rel}` is declared in .oss.json's version_sites and this file "
            "has no reader for it. Add one to READERS -- a declared site with "
            "no reader is a site with no guard, which is how the README badge "
            "reached fifteen releases stale.")
    path = root / rel
    if not path.exists():
        return None, f"`{rel}` is declared in version_sites but does not exist"
    version = reader(path.read_text(encoding="utf-8"))
    if version is None:
        return None, (
            f"`{rel}` exists but no version could be read out of it. That is a "
            "finding, not agreement: the reader looked and found nothing, "
            "which is exactly what a site renamed or reformatted looks like.")
    return version, None


def disagreements(root: Path, expected: str):
    """Every site that does not agree with `expected`, and every site that
    could not be read. Both are findings; neither is silence."""
    found = []
    for rel in declared_sites(root):
        version, problem = read_site(root, rel)
        if problem is not None:
            found.append(problem)
        elif version != expected:
            found.append(
                f"`{rel}` declares version {version!r}, but the code says "
                f"{expected!r}")
    return found


# --- the list itself, so an empty sweep cannot read as a clean one ----------


def test_the_declared_site_list_is_not_empty() -> None:
    """A loop over nothing passes. That must not be reachable."""
    assert declared_sites(), (
        "`.oss.json` declares no version_sites, so every assertion below "
        "iterates an empty list and reports a pass. Either the key was "
        "removed or the config moved; both are findings.")


def test_every_declared_site_has_a_reader() -> None:
    """Declaring a sixth site is what this test exists to catch."""
    missing = [rel for rel in declared_sites() if rel not in READERS]
    assert not missing, (
        f"version_sites declares {missing} with no reader in this file. A new "
        "version site is guarded by nothing until one is written -- add the "
        "reader in the same commit that adds the site.")


def test_the_known_readers_are_all_still_declared() -> None:
    """The reverse drift: a reader kept for a site nobody declares any more.

    Not fatal on its own, but it means this file believes it is guarding
    something the config has stopped listing, and the count of guarded sites
    silently overstates.
    """
    declared = set(declared_sites())
    stale = sorted(set(READERS) - declared)
    assert not stale, (
        f"this file carries readers for {stale}, which `.oss.json` no longer "
        "declares as version sites. Remove the reader or restore the "
        "declaration -- a guard over a site nobody lists is not coverage.")


# --- the sites agree with each other ---------------------------------------


@pytest.mark.parametrize("rel", declared_sites())
def test_declared_site_agrees_with_the_code(rel: str) -> None:
    """Each site, named individually so a red says which one drifted."""
    version, problem = read_site(ROOT, rel)
    assert problem is None, problem
    assert version == supertool.VERSION, (
        f"`{rel}` declares version {version!r} but supertool.VERSION is "
        f"{supertool.VERSION!r}. A release bump moves every site in "
        "`.oss.json`'s version_sites together, or it moves none of them.")


def test_no_declared_site_disagrees() -> None:
    """The same check as one whole verdict, which is what a release wants."""
    found = disagreements(ROOT, supertool.VERSION)
    assert not found, "version sites disagree:\n" + "\n".join(found)


# --- the control: a frozen site must be caught ------------------------------


def _tree(tmp_path: Path, versions: dict) -> Path:
    """A repository-shaped tree declaring all five sites, each at a given
    version. Read back through the same readers this file uses in anger."""
    root = tmp_path / "repo"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".oss.json").write_text(
        json.dumps({"version_sites": [
            ".claude-plugin/plugin.json", "_supertool.py", "pyproject.toml",
            "CHANGELOG.md", "README.md"]}), encoding="utf-8")
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": versions[".claude-plugin/plugin.json"]}),
        encoding="utf-8")
    (root / "_supertool.py").write_text(
        'VERSION = "%s"\n' % versions["_supertool.py"], encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nversion = "%s"\n' % versions["pyproject.toml"],
        encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n## [%s] - 2026-01-01\n" % versions["CHANGELOG.md"],
        encoding="utf-8")
    (root / "README.md").write_text(
        "[![Version](https://img.shields.io/badge/version-%s-orange)](x)\n"
        % versions["README.md"], encoding="utf-8")
    return root


_AGREED = {rel: "9.9.9" for rel in (
    ".claude-plugin/plugin.json", "_supertool.py", "pyproject.toml",
    "CHANGELOG.md", "README.md")}


def test_all_five_agreeing_is_clean(tmp_path: Path) -> None:
    """The positive control the negative one below needs.

    Without it, "no disagreements" could be a checker that never runs, and the
    frozen-site assertion would be the only thing keeping this file honest --
    which is a harness that can pass by being broken in one direction.
    """
    assert disagreements(_tree(tmp_path, _AGREED), "9.9.9") == []


@pytest.mark.parametrize("frozen", sorted(_AGREED))
def test_a_frozen_site_is_caught(tmp_path: Path, frozen: str) -> None:
    """#1854's non-optional control, run against every site rather than one.

    Four sites agree and the fifth is frozen at an older version -- the exact
    state the README badge was in for fifteen releases. Parametrised because a
    checker that reaches four of five passes the single-site version of this
    control four times out of five, and the one site it cannot see is by
    definition the one nobody picked.
    """
    versions = dict(_AGREED, **{frozen: "0.14.1"})
    found = disagreements(_tree(tmp_path, versions), "9.9.9")
    assert len(found) == 1, (
        f"freezing `{frozen}` at 0.14.1 while the other four say 9.9.9 "
        f"produced {found!r}. Exactly one disagreement is expected; zero means "
        "the checker never reached that site, which is the defect #1854 is "
        "about.")
    assert frozen in found[0] and "0.14.1" in found[0], found


def test_an_unreadable_site_is_a_finding_not_agreement(tmp_path: Path) -> None:
    """The absence this repository keeps filing, in its own shape here.

    A site whose reader matches nothing must not be counted as agreeing. It is
    the same failure as a grep that truncated silently: the checker produced
    the absence, and reading it as a clean result puts it in the world.
    """
    root = _tree(tmp_path, _AGREED)
    (root / "README.md").write_text("no badge here at all\n", encoding="utf-8")
    found = disagreements(root, "9.9.9")
    assert len(found) == 1 and "README.md" in found[0], found
    assert "finding, not agreement" in found[0], (
        "an unreadable site must say so in its own words, not be silently "
        f"folded into a version mismatch: {found!r}")


def test_a_declared_site_with_no_reader_is_a_finding(tmp_path: Path) -> None:
    """The sixth-site case, end to end rather than only as a name check."""
    root = _tree(tmp_path, _AGREED)
    config = json.loads((root / ".oss.json").read_text(encoding="utf-8"))
    config["version_sites"].append("docs/install.md")
    (root / ".oss.json").write_text(json.dumps(config), encoding="utf-8")
    found = disagreements(root, "9.9.9")
    assert len(found) == 1 and "docs/install.md" in found[0], found
    assert "no reader" in found[0], found
