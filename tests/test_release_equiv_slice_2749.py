"""The release workflow's equivalence slice names no preset the release tree drops (#2749).

`release-branch.yml` proves the stripped tree behaves like the source by running a
slice of this suite inside the built tree. It only runs on a tag. #2734 denied
`devto`, `bluesky`, `hashnode`, `slack` and `youtube` from that tree, and the slice
still listed `tests/test_preset_loader.py`, whose fixtures load three of them: the
v0.66.0 tag failed `verify` with 9 FileNotFoundError and published nothing.

This runs on every pull request instead: it reads the slice out of the workflow and
the dropped presets out of `.github/release-branch.json`, so the next deny-list
change that strands a slice file turns a PR red, not a tag.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release-branch.yml"
CONFIG = ROOT / ".github" / "release-branch.json"
QUOTED = "[\"'/]"


def _slice() -> list:
    text = WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"select=\((.*?)\n\s*\)", text, re.S)
    assert block, "no select=( ... ) array in release-branch.yml -- re-derive this test"
    return re.findall(r'"(tests/[^"]+[.]py)"', block.group(1))


def _dropped_presets() -> list:
    deny = json.loads(CONFIG.read_text(encoding="utf-8"))["deny"]
    return sorted(m.group(1) for d in deny if (m := re.fullmatch(r"presets/([a-z0-9_-]+)/", d)))


def _names(text: str, preset: str) -> bool:
    return re.search(QUOTED + re.escape(preset) + QUOTED, text) is not None


def test_the_readers_find_what_they_read() -> None:
    files = _slice()
    assert len(files) >= 10 and all((ROOT / f).is_file() for f in files), files
    assert {"devto", "bluesky", "hashnode", "slack", "youtube"} <= set(_dropped_presets())


def test_no_slice_file_names_a_dropped_preset() -> None:
    dropped = _dropped_presets()
    hits = {}
    for f in _slice():
        text = (ROOT / f).read_text(encoding="utf-8")
        named = [p for p in dropped if _names(text, p)]
        if named:
            hits[f] = named
    assert not hits, f"slice files that load presets the release tree drops: {hits}"


def test_the_check_fires_on_the_file_that_broke_v0660() -> None:
    text = (ROOT / "tests" / "test_preset_loader.py").read_text(encoding="utf-8")
    assert _names(text, "devto"), "positive control lost -- re-derive"
