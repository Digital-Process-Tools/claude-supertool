"""A fragment's issue number lives in its filename, and assembly deletes the filename (#1251).

`changelog.d/<issue>.<section>.md` carries the issue number in exactly one
structural place: the name. `assemble_changelog.py` writes the fragment's
*body* into the release section and deletes the file, so the number survives
the operation only if the author happened to type it into the prose.

Measured on this repo, on the fragment bodies as they stood at the commit
before each release:

- **v0.32.0** — 8 of 20 consumed fragments never named their own issue
  (1186, 1192, 1197, 1200, 1205, 1206, 1218, 1246). Of those, 7 would have
  reddened `assert_change_is_findable`; #1197 passed only because a
  *different* fragment in the same release happened to mention it, which is
  not a property anyone controls.
- **v0.33.0** — 6 of 28 (1220, 1230, 1241, 1254, 1259, 1261).

Two of the twenty had a `test_the_change_is_findable`, so the invariant was
enforced on 2 of 20 by author habit. This closes it at the other end: the
fragment is refused at write time, in the author's own PR, where a missing
`(#N)` costs one line instead of thirteen red legs on a release commit.

The check is deliberately ordered **before** `scan_fragment_body`, which is
the arm that needs `markdown-it-py`. A definite finding that needs no parser
must not be lost behind a `CannotValidate` — the same argument
`validators/changelog-fragment/changelog-fragment.py` already makes about
staging its own checks.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "assemble_changelog.py"
ADAPTER = REPO / "validators" / "changelog-fragment" / "changelog-fragment.py"

_spec = importlib.util.spec_from_file_location("assemble_changelog_1251", SCRIPT)
assert _spec is not None and _spec.loader is not None
asm = importlib.util.module_from_spec(_spec)
sys.modules["assemble_changelog_1251"] = asm
_spec.loader.exec_module(asm)

CITES = "- **A thing** ([#1192](https://example.invalid/issues/1192)). Prose.\n"
SILENT = "- **A thing** ([#1184](https://example.invalid/issues/1184)). Prose.\n"


def _frag_dir(tmp_path: Path, name: str, body: str) -> Path:
    directory = tmp_path / "changelog.d"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")
    return directory


def test_a_fragment_that_never_names_its_own_issue_is_refused(tmp_path: Path) -> None:
    directory = _frag_dir(tmp_path, "1192.security.md", SILENT)
    with pytest.raises(asm.BadFragment) as excinfo:
        asm.collect(directory)
    message = str(excinfo.value)
    assert message.startswith("1192.security.md:1: "), message
    assert "#1192" in message
    assert "1251" in message, "the finding should say where the rule came from"


def test_a_fragment_that_names_its_own_issue_is_collected(tmp_path: Path) -> None:
    directory = _frag_dir(tmp_path, "1192.security.md", CITES)
    assert [f.issue for f in asm.collect(directory)] == [1192]


def test_a_url_reference_counts_as_naming_the_issue(tmp_path: Path) -> None:
    body = "- **A thing** ([the issue](https://example.invalid/issues/1192)). Prose.\n"
    directory = _frag_dir(tmp_path, "1192.security.md", body)
    assert [f.issue for f in asm.collect(directory)] == [1192]


def test_a_longer_number_containing_this_one_is_not_a_reference(tmp_path: Path) -> None:
    """`#11920` is not a reference to #1192, and `_1130.py` is not one to #1130.

    The second is not hypothetical: v0.32.0's #1130 fragment named no issue at
    all and was findable only because it cited
    `tests/test_preset_git_splitlines_register_1130.py`. A grep for the digits
    finds that; a reader looking for the issue does not, and an author cannot
    aim at it. The rule has to be one somebody can satisfy on purpose.
    """
    body = "- **A thing** (#11920), see `tests/test_x_1192.py` and 21192. Prose.\n"
    directory = _frag_dir(tmp_path, "1192.security.md", body)
    with pytest.raises(asm.BadFragment) as excinfo:
        asm.collect(directory)
    assert "1192.security.md:1: " in str(excinfo.value)


def test_the_named_line_is_the_first_line_with_content(tmp_path: Path) -> None:
    directory = _frag_dir(tmp_path, "1192.security.md", "\n\n" + SILENT)
    with pytest.raises(asm.BadFragment) as excinfo:
        asm.collect(directory)
    assert str(excinfo.value).startswith("1192.security.md:3: ")


def test_the_finding_survives_an_absent_markdown_parser(monkeypatch, tmp_path: Path) -> None:
    """Ordered before the parser arm, so `CannotValidate` cannot swallow it."""
    monkeypatch.setattr(asm, "_MarkdownIt", None, raising=False)
    monkeypatch.setattr(asm, "_MD_IMPORT_ERROR", ImportError("no markdown_it"), raising=False)
    directory = _frag_dir(tmp_path, "1192.security.md", SILENT)
    with pytest.raises(asm.BadFragment) as excinfo:
        asm.collect(directory)
    assert "#1192" in str(excinfo.value)


def test_self_reference_finding_is_none_when_the_name_does_not_parse() -> None:
    """`collect` reports a bad name from `parse_fragment_name` and nothing else.

    A file called `notes.md` has no issue number to be missing, and inventing a
    second complaint about it would give the adapter and `--check` different
    counts for one file.
    """
    assert asm.self_reference_finding("notes.md", "- x\n") is None


def _adapter_verdict(tmp_path: Path, name: str, body: str) -> dict:
    scripts = tmp_path / ".github" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy(str(SCRIPT), str(scripts / "assemble_changelog.py"))
    directory = _frag_dir(tmp_path, name, body)
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), str(directory / name)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_write_time_validator_reports_the_same_finding(tmp_path: Path) -> None:
    """The adapter mirrors `collect`'s stages; a stage it skips is a rule that
    only fires in CI, which is #1132 inverted."""
    verdict = _adapter_verdict(tmp_path, "1192.security.md", SILENT)
    assert verdict.get("ok") is False, verdict
    assert verdict["errors"][0]["line"] == 1
    assert "#1192" in verdict["errors"][0]["msg"]


def test_the_write_time_validator_passes_a_fragment_that_cites_itself(tmp_path: Path) -> None:
    verdict = _adapter_verdict(tmp_path, "1192.security.md", CITES)
    assert verdict.get("ok") is True or verdict.get("skipped"), verdict
