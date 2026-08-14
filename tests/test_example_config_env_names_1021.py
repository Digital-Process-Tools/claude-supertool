"""#1021 -- an op entry's prose must name the env var the runner really sets.

`_run_preset_op` exports each non-reserved key of the entry as
`SUPERTOOL_<KEY.upper()>`. The **key alone** is uppercased; the op name is not
prepended. `.supertool.example.json`'s `ops.dashboard._doc` said

    Passes through as SUPERTOOL_DASHBOARD_LANE_PREFIX.

and the preset reads `SUPERTOOL_LANE_PREFIX`. Someone preferring an env
override to a config edit exports the documented name, gets no error, and
`dashboard` correctly prints `!! unread -- no lane vocabulary configured`: the
op is right and the reader has been told the op is broken. Misreports, on the
artifact rather than on the code.

The mistake is copy-paste-shaped -- the sibling `ops.radar._doc` right below it
is correct, but only because its key is `radar_tiers`, not because the op name
is prepended -- so the pin is the **rule**, swept over every entry of both
config files, and not the one sentence.

A bare `SUPERTOOL_*` is a generic mention of the mechanism rather than a name,
and is not a claim about any key; the pattern requires at least one character
after the underscore, so it never matches.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

import pytest

from _changelog_findable import assert_change_is_findable

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Keys `_run_preset_op` consumes itself instead of exporting. Mirrored rather
#: than imported: `_RESERVED_KEYS` is a local inside that function.
RESERVED = frozenset({
    "cmd", "timeout", "description", "syntax", "example", "status",
    "restartMcp", "replaces", "paths", "exitStatus",
})

_ENV = re.compile(r"SUPERTOOL_[A-Z0-9_]+")

CONFIGS = (".supertool.json", ".supertool.example.json")


def _entries(name: str) -> Dict[str, dict]:
    raw = json.loads((REPO_ROOT / name).read_text(encoding="utf-8"))
    return {k: v for k, v in (raw.get("ops") or {}).items()
            if isinstance(v, dict)}


@pytest.mark.parametrize("config", CONFIGS)
def test_every_env_name_in_prose_is_one_the_runner_sets(config: str) -> None:
    wrong: List[str] = []
    for op, entry in _entries(config).items():
        prose = " ".join(v for k, v in entry.items() if isinstance(v, str))
        for named in sorted(set(_ENV.findall(prose))):
            key = named[len("SUPERTOOL_"):].lower()
            if key in RESERVED:
                wrong.append(
                    "{0}: ops.{1} documents {2}, but '{3}' is a reserved key "
                    "and is never exported".format(config, op, named, key))
            elif key not in entry:
                wrong.append(
                    "{0}: ops.{1} documents {2}, but the runner exports "
                    "SUPERTOOL_<KEY.upper()> for each key of the entry and "
                    "there is no '{3}' key -- candidates: {4}".format(
                        config, op, named, key,
                        ", ".join(sorted(k for k in entry
                                         if k not in RESERVED
                                         and not k.startswith("_")))))
    assert not wrong, "\n".join(wrong)


def test_the_dashboard_entry_names_the_variable_the_preset_reads() -> None:
    """The filed instance, kept as a named regression next to the sweep."""
    entry = _entries(".supertool.example.json")["dashboard"]
    doc = entry["_doc"]
    assert "SUPERTOOL_LANE_PREFIX" in doc, doc
    assert "SUPERTOOL_DASHBOARD_LANE_PREFIX" not in doc, doc


def test_a_changelog_fragment_exists() -> None:
    assert_change_is_findable(1021)
