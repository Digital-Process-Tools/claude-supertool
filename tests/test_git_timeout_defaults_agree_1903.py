"""SUPERTOOL_GIT_TIMEOUT's three defaults and their prose must agree (#1903).

`SUPERTOOL_GIT_TIMEOUT` is one env var read at three sites with three
different defaults: `presets/git/status.py` (5s), `presets/git/_git_common
.git_timeout()` (10s, shared by six presets) and `validators/git-status
/git-status.py` (15s). #1886 was filed because the docs described only two
of them and got the third wrong; PR #1900 fixed the prose at both doc sites
that assert all three -- `docs/validators.md` and
`validators/git-status/README.md`. Nothing joined that prose to the code, so
the next time one of the three numbers moves, the docs go stale exactly the
way they did the first time, silently. This file is that join, the same
shape as `.oss.json`'s `version_sites` / `test_version_sites_agree_1854.py`
one variable over.

**Route chosen, and why.** Three routes were available: grep a literal out
of source, import the module, or parse the prose table. A prose-only test
passes when the code moves and a code-only test never touches the docs, so
neither alone closes this issue -- it has to read both and compare them.
Grepping the code for a bare literal risks matching the wrong `5`/`10`/`15`
in the file (line numbers, timeouts for other things); importing the module
via `importlib` and reading its named constant is what
`test_git_timeout_disclosure_650.py` and
`test_git_status_validator_timeout_1882.py` already do for exactly this
reason, and it costs nothing at import time -- none of the three modules
spawns git or does other work merely by being loaded, only when their
functions are called. So: import for the code side, regex for the prose
side, because the code side has a name to import and the prose side does
not.

**The third state.** A prose sentence that no longer matches the regex --
because the wording moved, not just the number -- must not read as
agreement. Every extraction below is a `pytest.fail` with the searched text
quoted, never a silent skip or a coerced default, so a reader who renames
the site in prose gets a loud "could not find" rather than a quiet pass.
That is also how a *fourth* claimed site would be caught: each prose blob is
asserted to carry exactly three `N` + `s` numbers tied to the three known
sites, so a stray fourth number changes the count and fails loudly rather
than being ignored because only three named patterns were searched for.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _load(name: str, relpath: str):
    path = _ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, relpath
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def status_default() -> int:
    mod = _load("git_timeout_1903_status", "presets/git/status.py")
    return mod._GIT_TIMEOUT_DEFAULT


@pytest.fixture(scope="module")
def common_default() -> int:
    mod = _load("git_timeout_1903_common", "presets/git/_git_common.py")
    return mod._GIT_TIMEOUT_DEFAULT


@pytest.fixture(scope="module")
def validator_default() -> int:
    mod = _load(
        "git_timeout_1903_validator", "validators/git-status/git-status.py"
    )
    return mod.GIT_TIMEOUT_DEFAULT


def _find_one(pattern: str, text: str, where: str) -> int:
    """One capture, exactly once -- ambiguous or absent both fail loudly."""
    matches = re.findall(pattern, text)
    if not matches:
        pytest.fail(
            f"could not find {pattern!r} in {where} -- prose moved, "
            "regex needs updating (not the code)"
        )
    if len(matches) > 1:
        pytest.fail(
            f"{pattern!r} matched {len(matches)} times in {where}, expected "
            f"exactly once: {matches}"
        )
    return int(matches[0])


def _bare_number_seconds(text: str) -> list[str]:
    """Every standalone N+'s' token, the sanity count against a 4th site."""
    return re.findall(r"(?<!\d)(\d+)s(?!\w)", text)


# --- docs/validators.md ------------------------------------------------

_VALIDATORS_MD_STATUS = r"`presets/git/status\.py` gives its own courtesy calls \*\*(\d+)s\*\*"
_VALIDATORS_MD_COMMON = r"`presets/git/_git_common\.git_timeout\(\)`'s \*\*(\d+)s\*\*"
_VALIDATORS_MD_ADAPTER = r"This adapter's \*\*(\d+)s\*\* sits above both"


@pytest.fixture(scope="module")
def validators_md_bullet() -> str:
    text = (_ROOT / "docs" / "validators.md").read_text(encoding="utf-8")
    # The bullet begins with this sentinel and runs to end of line -- it is
    # one long paragraph in the source, so "the bullet" is "the line".
    for line in text.splitlines():
        if "one env var read at three sites with three different defaults" in line:
            return line
    pytest.fail(
        "docs/validators.md no longer has the "
        "'one env var read at three sites' bullet -- #1903's join has "
        "nothing to compare against"
    )


def test_validators_md_status_default_matches_code(validators_md_bullet, status_default):
    doc_value = _find_one(_VALIDATORS_MD_STATUS, validators_md_bullet, "docs/validators.md")
    assert doc_value == status_default, (
        f"docs/validators.md claims presets/git/status.py defaults to "
        f"{doc_value}s; presets.git.status._GIT_TIMEOUT_DEFAULT is "
        f"{status_default}"
    )


def test_validators_md_common_default_matches_code(validators_md_bullet, common_default):
    doc_value = _find_one(_VALIDATORS_MD_COMMON, validators_md_bullet, "docs/validators.md")
    assert doc_value == common_default, (
        f"docs/validators.md claims presets/git/_git_common.git_timeout() "
        f"defaults to {doc_value}s; presets.git._git_common._GIT_TIMEOUT_DEFAULT "
        f"is {common_default}"
    )


def test_validators_md_validator_default_matches_code(validators_md_bullet, validator_default):
    doc_value = _find_one(_VALIDATORS_MD_ADAPTER, validators_md_bullet, "docs/validators.md")
    assert doc_value == validator_default, (
        f"docs/validators.md claims the git-status validator defaults to "
        f"{doc_value}s; validators/git-status/git-status.py's "
        f"GIT_TIMEOUT_DEFAULT is {validator_default}"
    )


def test_validators_md_bullet_names_exactly_three_sites(validators_md_bullet):
    """A fourth `**Ns**` in this bullet is a fourth claimed site -- catch it."""
    bold_seconds = re.findall(r"\*\*(\d+)s\*\*", validators_md_bullet)
    assert len(bold_seconds) == 3, (
        f"docs/validators.md's timeout bullet carries {len(bold_seconds)} "
        f"bolded 'Ns' defaults ({bold_seconds}), expected exactly 3 -- a "
        "site was added or removed without this test being told about it"
    )


# --- validators/git-status/README.md ------------------------------------

_README_STATUS = r"`presets/git/status\.py`'s courtesy calls default to (\d+)s"
_README_COMMON = r"`_git_common\.git_timeout\(\)`'s (\d+)s"
_README_ADAPTER = r"this adapter's own is (\d+)s"


@pytest.fixture(scope="module")
def readme_line() -> str:
    text = (_ROOT / "validators" / "git-status" / "README.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if "The same variable, three different defaults" in line:
            return line
    pytest.fail(
        "validators/git-status/README.md no longer has the "
        "'The same variable, three different defaults' sentence -- #1903's "
        "join has nothing to compare against"
    )


def test_readme_status_default_matches_code(readme_line, status_default):
    doc_value = _find_one(_README_STATUS, readme_line, "validators/git-status/README.md")
    assert doc_value == status_default, (
        f"validators/git-status/README.md claims presets/git/status.py "
        f"defaults to {doc_value}s; presets.git.status._GIT_TIMEOUT_DEFAULT "
        f"is {status_default}"
    )


def test_readme_common_default_matches_code(readme_line, common_default):
    doc_value = _find_one(_README_COMMON, readme_line, "validators/git-status/README.md")
    assert doc_value == common_default, (
        f"validators/git-status/README.md claims "
        f"_git_common.git_timeout() defaults to {doc_value}s; "
        f"presets.git._git_common._GIT_TIMEOUT_DEFAULT is {common_default}"
    )


def test_readme_validator_default_matches_code(readme_line, validator_default):
    doc_value = _find_one(_README_ADAPTER, readme_line, "validators/git-status/README.md")
    assert doc_value == validator_default, (
        f"validators/git-status/README.md claims this adapter defaults to "
        f"{doc_value}s; validators/git-status/git-status.py's "
        f"GIT_TIMEOUT_DEFAULT is {validator_default}"
    )


def test_readme_line_names_exactly_three_sites(readme_line):
    """A fourth bare 'Ns' on this line is a fourth claimed site -- catch it."""
    # Scope to the sentence itself, not the whole table row (the row's
    # "Default" column cell is "`15`" with no trailing 's', so it is already
    # excluded by requiring the 's' suffix -- this slice just drops the
    # unrelated prose after the three defaults, e.g. "one fast one").
    start = readme_line.index("The same variable, three different defaults")
    sentence = readme_line[start:]
    bare_seconds = _bare_number_seconds(sentence)
    assert len(bare_seconds) == 3, (
        f"validators/git-status/README.md's timeout sentence carries "
        f"{len(bare_seconds)} bare 'Ns' defaults ({bare_seconds}), expected "
        "exactly 3 -- a site was added or removed without this test being "
        "told about it"
    )


# --- cross-check: the two doc sites must not merely both match the code,
#     they must agree with each other (a third, independent way to catch a
#     drift where one doc site was fixed and the other was not). ----------

def test_both_doc_sites_agree_on_all_three_numbers(validators_md_bullet, readme_line):
    md_status = _find_one(_VALIDATORS_MD_STATUS, validators_md_bullet, "docs/validators.md")
    md_common = _find_one(_VALIDATORS_MD_COMMON, validators_md_bullet, "docs/validators.md")
    md_adapter = _find_one(_VALIDATORS_MD_ADAPTER, validators_md_bullet, "docs/validators.md")
    rd_status = _find_one(_README_STATUS, readme_line, "validators/git-status/README.md")
    rd_common = _find_one(_README_COMMON, readme_line, "validators/git-status/README.md")
    rd_adapter = _find_one(_README_ADAPTER, readme_line, "validators/git-status/README.md")
    assert (md_status, md_common, md_adapter) == (rd_status, rd_common, rd_adapter), (
        "docs/validators.md and validators/git-status/README.md disagree on "
        f"the three defaults: {(md_status, md_common, md_adapter)} vs "
        f"{(rd_status, rd_common, rd_adapter)}"
    )
