"""#1203 — tomllint ships here and did not run here.

`.supertool.example.json` has offered a `tomllint` entry since the adapter
landed; `.supertool.json` had none. So the repository that distributes the TOML
checker was the one repository it never checked, and `pyproject.toml` — one of
the five version sites a release has to bump, guarded by
`tests/test_pyproject_version_522.py` precisely because getting it wrong ships a
release that reaches nobody — was edited with no validator matching it.

Same shape as #833, one file type over: a checker nobody is checking has no
feedback loop, and its defects surface in somebody elses repository. That is
exactly how #1157 was found.

Modelled on `tests/test_html_check_registration_833.py`, and deliberately not
generalised into "every adapter must be registered here" for the reason that
file gives: this is a Python repo, and `phplint`/`phpstan`/`phpmd` are correctly
unregistered. The registration decision is per file type, so the test is too.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import supertool  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED = json.loads((REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))
EXAMPLE = json.loads((REPO_ROOT / ".supertool.example.json").read_text(encoding="utf-8"))

ADAPTER = REPO_ROOT / "validators" / "tomllint" / "tomllint.py"


def test_shipped_config_dispatches_a_toml_file_to_tomllint(monkeypatch):
    """The core matcher, run against this repo real config."""
    monkeypatch.setattr(supertool, "_CONFIG", SHIPPED)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    names = set(supertool._applicable_validators("edit", "pyproject.toml"))
    assert "tomllint" in names, (
        "no validator in .supertool.json parses *.toml — an edit that breaks "
        "pyproject.toml validates clean in the repo that ships the TOML "
        "checker. Selected: %s" % sorted(names))


def test_shipped_registration_points_at_the_adapter_that_exists():
    spec = SHIPPED["validators"]["tomllint"]
    assert "validators/tomllint/tomllint.py" in spec["cmd"]
    assert ADAPTER.is_file()
    for op in ("edit", "replace", "replace_lines", "paste", "append", "vim"):
        assert op in spec["hooks_into"], op


def test_a_broken_toml_rolls_the_edit_back():
    """`rollback_on_fail` is the point, not a detail copied from the example.

    A TOML file that no longer parses is not a lint opinion — it is a file the
    next reader cannot load, and for `pyproject.toml` it is a release that does
    not build. Contrast `ruff`, registered here with rollback off, where a
    finding sits next to a working file.
    """
    assert SHIPPED["validators"]["tomllint"]["rollback_on_fail"] is True


def test_every_toml_file_in_this_repo_passes_the_validator_it_now_runs():
    """Registering a gate that immediately reddens the repo is a different
    decision from registering one that passes, and the maintainer asked which
    this was before it landed. It is the second — and this keeps it that way,
    which is the whole feedback loop the issue is about.
    """
    tracked = subprocess.run(["git", "ls-files", "*.toml"], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True,
                             encoding="utf-8", errors="replace")
    files = [f for f in tracked.stdout.split() if f]
    assert files, "no tracked .toml files — this test has stopped measuring anything"
    for rel in files:
        r = subprocess.run([sys.executable, str(ADAPTER), rel], cwd=REPO_ROOT,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = json.loads(r.stdout)
        assert "skipped" not in out, (
            f"{rel}: tomllint declined — no TOML parser on this Python, so "
            f"registration buys nothing here: {out['skipped']}")
        assert out["ok"] is True, f"{rel}: {out}"


def test_example_catalogue_still_offers_tomllint():
    """The catalogue docs/validators.md tells other projects to copy from."""
    spec = EXAMPLE["validators"]["tomllint"]
    assert spec["match"] == "*.toml"
    assert "validators/tomllint/tomllint.py" in spec["cmd"]
