"""#1285 (folding #1152) — no registry may name an op the dispatcher rejects.

`blame` sat in `_PARALLEL_SAFE_OPS`, `_PATH_ARG_POSITIONS` and `_READ_OP_TARGETS`
for three and a half months after b4099a5 moved it to the git preset as
`git-blame`. Four deletions were needed and three were missed.

Every one of those tables is consulted to answer a question *about an op* — is it
parallel-safe, which argument is a path, what does it target — so a name in them
with no dispatch branch means they are not derived from the dispatcher. The
immediate risk was nil, because an op that cannot be invoked cannot misbehave;
the cost is that they read as an inventory and are not one, and two of the three
are containment-adjacent. The day a preset or config op is named `blame`, those
rows apply to it.

PR #1418's `test_neither_table_names_an_op_that_does_not_dispatch` is the same
shape one surface along, and does not cover this: its denominator is the docs
page, not these tables.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import supertool

ROOT = Path(__file__).resolve().parent.parent

_REGISTRIES = {
    "_PARALLEL_SAFE_OPS": lambda: set(supertool._PARALLEL_SAFE_OPS),
    "_PATH_ARG_POSITIONS": lambda: set(supertool._PATH_ARG_POSITIONS),
    "_READ_OP_TARGETS": lambda: set(supertool._READ_OP_TARGETS),
    "_MAX_COLON_SLOTS": lambda: set(supertool._MAX_COLON_SLOTS),
    "_OP_SAFETY_BUILTIN": lambda: set(supertool._OP_SAFETY_BUILTIN),
}


def test_every_registry_is_non_empty() -> None:
    """A registry renamed out from under this file would empty the denominator.

    Without this the sweep below passes over nothing and reports a clean run,
    which is the absence-read-as-presence defect wearing a test.
    """
    for name, get in _REGISTRIES.items():
        assert len(get()) > 5, f"{name} collapsed — the sweep stopped sweeping"


def test_no_registry_names_an_op_the_dispatcher_rejects() -> None:
    """The structural half: every name is one this binary accepts."""
    accepted = set(supertool._valid_op_names())
    for name, get in _REGISTRIES.items():
        ghosts = sorted(get() - accepted)
        assert not ghosts, (
            f"{name} names {ghosts}, absent from _valid_op_names() — the table "
            f"reads as an inventory of ops and is not one"
        )


def test_no_registry_name_answers_unknown_operation() -> None:
    """The behavioural half, against dispatch itself rather than a derived set.

    `_valid_op_names()` is built from the same sets a drifting table could be
    edited alongside; `dispatch` is the thing a caller actually meets. Probed
    with an empty argument, which every op refuses on its own terms — the
    assertion is only that the refusal is not `unknown operation`.
    """
    for name, get in _REGISTRIES.items():
        for op in sorted(get()):
            if op in supertool._MAIN_LEVEL_OPS:
                continue  # honoured and stripped by main(), never reaching dispatch
            out = supertool.dispatch(f"{op}:")
            assert "unknown operation" not in out, (
                f"{name} names {op!r} and the dispatcher answers "
                f"'unknown operation' for it"
            )


#: The three places a project's own `builtin-ops` block can name a ghost op.
#: `presets/*.json` is #2080's addition -- #2025 made `builtin-ops` reachable
#: from a shipped preset manifest, and this parametrize stayed at the two
#: project config files it was written against in #1285, so every preset's
#: own `builtin-ops` (`lsp.json`, `vim.json`, and any future one) went
#: unguarded: `presets/ghost.json` carrying a `blame` entry passed this suite
#: outright before this line was added. Built once at collection time, not
#: cached, so a preset added later is swept without editing this file.
_BUILTIN_OP_CONFIGS = [
    ".supertool.json",
    ".supertool.example.json",
    *sorted(
        str(p.relative_to(ROOT))
        for p in (ROOT / "presets").glob("*.json")
    ),
]


@pytest.mark.parametrize("config", _BUILTIN_OP_CONFIGS)
def test_no_config_calls_a_non_builtin_a_builtin(config: str) -> None:
    """The fifth registry, and the only one a user copies (#1285).

    `.supertool.example.json` is the file this repo hands people to start from,
    and its `builtin-ops` block carried a `blame` entry — syntax, description
    and example — for an op the dispatcher rejects. Anyone who copied it got
    `blame` in their `ops` roster and a full `help:blame` reference for a name
    that answers `unknown operation`. `builtin-ops` is the one section that
    makes a claim about the *binary* rather than about the project, so it is
    the one section that can be wrong on its own.

    Checked against the head of each entry's `syntax`, not against its key.
    Two keys here are deliberately labels rather than op names — `grep-count`
    and `read-grep` document a *mode* of `grep` and of `read`, and their syntax
    says so. `blame`'s syntax said `blame:PATH:LINE[:N]`, which is the claim
    that was false.
    """
    data = json.loads((ROOT / config).read_text(encoding="utf-8"))
    accepted = set(supertool._valid_op_names())
    ghosts = []
    for key, spec in (data.get("builtin-ops") or {}).items():
        syntax = spec.get("syntax", "") if isinstance(spec, dict) else ""
        # `gc[:DAYS]` and friends put the optional-argument bracket straight
        # against the name, so `[` ends the op name as surely as `:` does.
        head = re.split(r"[:\s|\[]", syntax.strip(), maxsplit=1)[0]
        assert head, f"{config}: {key!r} has no syntax to check"
        if head not in accepted:
            ghosts.append(f"{key} (syntax names {head!r})")
    assert not ghosts, (
        f"{config} documents {ghosts} under 'builtin-ops' and this binary has "
        f"no such op — `ops` lists them and `help:` writes a reference for them"
    )
