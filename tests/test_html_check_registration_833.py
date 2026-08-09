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
    assert spec["match"] == "*.html"
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
