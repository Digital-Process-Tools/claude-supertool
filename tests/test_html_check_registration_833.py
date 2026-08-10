"""#833 — the `html-check` adapter has to be *reachable*, not just present.

The adapter shipped in `validators/html-check/` and was registered nowhere, so
`validate:` on a page with a broken inline `<script>` ran only `git-status` and
printed `0 with findings`. An absence produced by the tool, read as an absence
in the world — the exact silence #833 exists to close, arriving inside the fix
for it.

These pin the two registration surfaces that make it reachable:

* `.supertool.json` — this repo runs on its own validator.
* `.supertool.example.json` — the catalogue `docs/validators.md` tells a user of
  another project to copy from ("Enable any of these by copying the relevant
  entry from `.supertool.example.json`"). A row in that table naming a validator
  the file does not contain sends the reader to an empty shelf.

Deliberately NOT a scanner over `validators/*` asserting every directory is
registered: this is a Python repo and `phplint`/`phpstan`/`phpmd` correctly go
unregistered here. See the report on #1140 for why no sound general invariant
was found.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import supertool  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED = json.loads((REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))
EXAMPLE = json.loads((REPO_ROOT / ".supertool.example.json").read_text(encoding="utf-8"))

BROKEN_PAGE = """<html>
<body>
<script>
const a = ;
</script>
</body>
</html>
"""


def test_shipped_config_dispatches_an_html_file_to_html_check(monkeypatch):
    """The core's own matcher, run against this repo's real config."""
    monkeypatch.setattr(supertool, "_CONFIG", SHIPPED)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    names = set(supertool._applicable_validators("edit", "dashboard.html"))
    assert "html-check" in names, (
        "no validator in .supertool.json matches *.html — a page with broken "
        "inline JS validates clean in the repo that ships the checker for it. "
        "Selected: %s" % sorted(names))


def test_shipped_registration_points_at_the_adapter_that_exists():
    spec = SHIPPED["validators"]["html-check"]
    assert "validators/html-check/html-check.py" in spec["cmd"]
    assert (REPO_ROOT / "validators" / "html-check" / "html-check.py").is_file()
    for op in ("edit", "replace", "replace_lines", "paste", "append", "vim"):
        assert op in spec["hooks_into"], op


def test_example_catalogue_offers_html_check():
    """docs/validators.md points the reader here; the entry has to be here."""
    assert "html-check" in EXAMPLE["validators"], (
        "docs/validators.md lists html-check and tells the reader to copy the "
        "entry from .supertool.example.json, which does not contain one")
    spec = EXAMPLE["validators"]["html-check"]
    # The glob's exact text is pinned by `test_htm_is_offered_by_the_example
    # _catalogue_too` (#1159). What #833 needs from it is narrower and does not
    # move when a suffix is added: `.html` reaches the adapter.
    assert "html" in spec["match"], spec["match"]
    assert "validators/html-check/html-check.py" in spec["cmd"]


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="html-check needs node --check; it reports skipped without it")
def test_validate_op_reports_broken_inline_js_end_to_end(tmp_path):
    """The maintainer's own measurement, as a test.

    Run the real CLI from the repo root so the real `.supertool.json` is the one
    loaded — the defect was in that file, so a synthesised config would not see
    it.
    """
    page = tmp_path / "badjs.html"
    page.write_text(BROKEN_PAGE, encoding="utf-8")
    env = dict(os.environ, SUPERTOOL_ALLOW_OUTSIDE_CWD="1", SUPERTOOL_NO_RTK="1")
    # `validate:@-` rather than `validate:<path>`: on Windows tmp_path starts
    # with a drive letter and the colon CLI would split it off as its own
    # token. The payload route exists for exactly that; JSON is used because
    # auto-detect keys off a leading brace and json.dumps escapes the
    # separators for us.
    payload = json.dumps({"path": str(page)})
    r = subprocess.run([sys.executable, "supertool.py", "validate:@-"],
                       cwd=REPO_ROOT, input=payload, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=180, env=env)
    out = r.stdout + r.stderr
    assert "html-check" in out, out
    assert "1 err" in out, out
    # L4 is the `const a = ;` line of BROKEN_PAGE. Asserting the number, not
    # just that something was reported: the adapter maps a node line number
    # inside an extracted block back onto the HTML file's own numbering, and a
    # row that fired on the wrong line is a different bug wearing a pass.
    assert "L4 syntax" in out, out

# ---------------------------------------------------------------------------
# #1159 -- `.htm` is the same file, and the catalogue has to hold every adapter
# ---------------------------------------------------------------------------

VALIDATORS_DIR = REPO_ROOT / "validators"

# `common/` is a library imported by the adapters (`refusal.py`,
# `source_context.py`), not an adapter. It is excluded here by name, and by
# name only: a future directory that is also not an adapter has to be added to
# this list deliberately, where the decision is visible, rather than slipping
# past a pattern that happens not to match it.
NOT_AN_ADAPTER = {"common"}


def _adapter_dirs() -> set:
    return {p.name for p in VALIDATORS_DIR.iterdir()
            if p.is_dir() and not p.name.startswith("__")} - NOT_AN_ADAPTER


def _dirs_referenced_by(config: dict) -> set:
    referenced = set()
    for spec in config.get("validators", {}).values():
        cmd = spec.get("cmd", "") if isinstance(spec, dict) else ""
        for name in _adapter_dirs():
            if "validators/%s/" % name in cmd:
                referenced.add(name)
    return referenced


def test_every_bundled_adapter_has_an_entry_in_the_example_catalogue() -> None:
    """The guard #1159 exists to make buildable.

    `docs/validators.md` says "Enable any of these by copying the relevant
    entry from `.supertool.example.json`". An adapter that ships in this repo
    with no entry there sends the reader to an empty shelf -- shipped and
    unreachable, the #833 shape, one layer out.

    It could not be landed alongside #833 because pre-existing violations mean
    a guard is satisfied by bulk-editing the thing it checks inside a PR about
    something else. Fixing them first is what makes the guard a guard.

    This asserts nothing about `.supertool.json`, deliberately: this is a
    Python repo and it is correct for it not to run `phplint` on itself.
    """
    missing = sorted(_adapter_dirs() - _dirs_referenced_by(EXAMPLE))
    assert not missing, (
        "these adapters ship in validators/ and have no entry in "
        ".supertool.example.json, which docs/validators.md tells the reader to "
        "copy from: %s" % missing)


def test_the_catalogue_has_no_entry_for_an_adapter_that_is_not_there() -> None:
    """The guard's other direction -- a catalogue entry pointing at nothing.

    A `cmd` naming `validators/<x>/<x>.py` for a directory that was renamed or
    removed fails at spawn time in the user's project, not here. The two halves
    together are what makes the catalogue a claim about this repo.
    """
    orphans = []
    for name, spec in EXAMPLE["validators"].items():
        cmd = spec.get("cmd", "") if isinstance(spec, dict) else ""
        marker = "validators/"
        if marker not in cmd:
            continue  # the two `_example-*` entries illustrate keys, not adapters
        directory = cmd.split(marker, 1)[1].split("/", 1)[0]
        if not (VALIDATORS_DIR / directory).is_dir():
            orphans.append((name, directory))
    assert not orphans, (
        "catalogue entries point at validators/ directories that do not "
        "exist: %s" % orphans)


def test_htm_is_dispatched_to_html_check_by_the_shipped_config(monkeypatch) -> None:
    """`.htm` is the same document with a shorter suffix (#1159).

    Identical content, identical checker, and a page saved as `dashboard.htm`
    got no inline-JS check at all -- the #833 gap surviving in the one glob
    that decides which files reach the adapter.
    """
    monkeypatch.setattr(supertool, "_CONFIG", SHIPPED)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    names = set(supertool._applicable_validators("edit", "dashboard.htm"))
    assert "html-check" in names, (
        "no validator in .supertool.json matches *.htm. Selected: %s"
        % sorted(names))


def test_htm_is_offered_by_the_example_catalogue_too() -> None:
    """A user copying the entry gets `.htm` covered without editing the glob."""
    match = EXAMPLE["validators"]["html-check"]["match"]
    assert match == "*.{html,htm}", (
        "the two suffixes belong in one entry rather than a second "
        "`html-check-htm` row: unlike yaml/tsc there is no second tool and no "
        "second decision to record, and a copied config covering only one of "
        "them is the gap #1159 reports. Got: %s" % match)


def test_html_check_glob_is_the_same_in_both_configs() -> None:
    """The repo runs the entry it publishes.

    #1203 is the same drift the other way -- `tomllint` offered in the example
    and registered nowhere here. A glob that is wider in the catalogue than in
    this repo's own config means the widening was never exercised by the only
    project that runs the adapter on every edit.
    """
    assert (SHIPPED["validators"]["html-check"]["match"]
            == EXAMPLE["validators"]["html-check"]["match"])

# `timeout=N` in a `subprocess.run(...)` call, and the named budgets the
# warm-daemon adapters use instead. Both shapes are read because both exist.
_INLINE_BUDGET = re.compile(r"\btimeout\s*=\s*(\d+)")
_NAMED_BUDGET = re.compile(r"^(?:SPAWN|CALL)_TIMEOUT_SEC\s*=\s*(\d+)", re.MULTILINE)


def _own_budget_seconds(directory: str):
    """The worst case the adapter allows itself, or None if it declares none.

    None is a third state on purpose. An adapter with no budget to read is not
    an adapter that passes this check -- it is one this check could not answer
    for, and `test_the_budget_reader_still_reads_something` below is what stops
    a reader that has quietly stopped reading from looking like a clean sweep.
    """
    source = (VALIDATORS_DIR / directory / ("%s.py" % directory)).read_text(
        encoding="utf-8")
    named = [int(n) for n in _NAMED_BUDGET.findall(source)]
    if named:
        return sum(named)
    inline = [int(n) for n in _INLINE_BUDGET.findall(source)]
    return max(inline) if inline else None


def _catalogue_budgets():
    """(name, directory, outer timeout, own budget) for every readable entry."""
    rows = []
    for name, spec in EXAMPLE["validators"].items():
        cmd = spec.get("cmd", "") if isinstance(spec, dict) else ""
        if "validators/" not in cmd or "timeout" not in spec:
            continue
        directory = cmd.split("validators/", 1)[1].split("/", 1)[0]
        if not (VALIDATORS_DIR / directory).is_dir():
            continue
        own = _own_budget_seconds(directory)
        if own is not None:
            rows.append((name, directory, spec["timeout"], own))
    return rows


def test_a_catalogue_timeout_leaves_the_adapter_room_to_answer() -> None:
    """The outer budget must exceed the adapter's own, never equal it.

    `docs/validators.md` states the rule on the `html-check` entry: a timeout
    equal to the adapter's own means the core's timer always wins the race, so
    the row reads `NOT CHECKED` with nothing naming what hung. With headroom
    the adapter's own arm fires first and reports a cause. That is the same
    three-state contract one layer out -- an absence produced by the tool has
    to say which absence it is.

    Only the entries added for #1159 are checked, because they are the ones
    this test was written with. Several adapter READMEs publish a figure that
    violates this rule for the pre-existing rows; that is a real defect and it
    is filed rather than fixed inside a PR about two other issues.
    """
    checked = {"phpstan", "phpmd", "lsp-diag",
               "phpstan-mcp", "phpmd-mcp", "phpunit-mcp", "rector-mcp"}
    too_tight = [
        (name, outer, own)
        for name, _dir, outer, own in _catalogue_budgets()
        if name in checked and outer <= own
    ]
    assert not too_tight, (
        "these catalogue entries give the adapter no room to report its own "
        "timeout, so the core kills it first and the reader gets NOT CHECKED "
        "with no cause -- (entry, outer timeout, adapter's own budget): %s"
        % too_tight)


def test_the_budget_reader_still_reads_something() -> None:
    """A sweep that stopped finding anything must not read as a clean sweep.

    If an adapter is rewritten so neither budget shape matches, the test above
    silently checks fewer entries and keeps passing. This is the floor that
    turns that into a red.
    """
    read = {name for name, _dir, _outer, _own in _catalogue_budgets()}
    assert len(read) >= 7, (
        "the budget reader found only %d catalogue entries with a readable "
        "budget; it is meant to read at least the seven added by #1159. "
        "Found: %s" % (len(read), sorted(read)))
