"""#1245 — the registry is read by machines, so every name in it must resolve.

`.supertool.json`'s `builtin-ops` is the place a reader and a tool both go to
learn what this binary accepts. It carried `read-grep` and `grep-count`, which
are documentation for *forms* of `read` and `grep` — `read:PATH:::grep=P` and
`grep:P:PATH:L:C:count`. Neither is a dispatchable name: `grep-count:x` answers
`unknown operation`. #1231's roster had to exclude them by hand, which is the
tell that the declaration and the dispatcher had drifted.

A form entry is worth keeping — both carry `hint: true`, so their example
survives `ops-compact` and reaches the SessionStart listing, which is the one
place a non-obvious payload shape gets discovered. So they are kept and
**declared**: `"form": "grep"` says *this entry documents a form of `grep`, it
is not an op name*. The alternative considered was inferring it from the syntax
head, and inference is what this file exists to stop.

The count claim (#1245's third item) is not tested here. It was already fixed —
`docs/operations/index.md` opens with the real number — and
`tests/test_ops_index_complete_1371.py` already holds it there, both directions,
plus a phantom-name check on that page. This file is the same guard one layer
down, on the registry rather than on the reference.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def shipped_config(monkeypatch: pytest.MonkeyPatch) -> dict:
    """This repo's own .supertool.json — conftest hands tests `_CONFIG = {}`."""
    cfg = json.loads((REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    supertool._merge_presets(cfg, str(REPO_ROOT))
    monkeypatch.setattr(supertool, "_CONFIG", cfg)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG_PATH",
                        str(REPO_ROOT / ".supertool.json"))
    return cfg


#: Both configs this repo ships. `.supertool.example.json` is the template a
#: user copies, so a phantom there is one that propagates — and it had a third,
#: `grep-no-exclude`, that #1245 did not count. Enumerating from the files
#: rather than from the issue body found it, which is the same reason
#: `test_ops_index_complete_1371` enumerates from the product.
SHIPPED_CONFIGS = (".supertool.json", ".supertool.example.json")


@pytest.mark.parametrize("filename", SHIPPED_CONFIGS)
def test_every_builtin_ops_entry_is_a_dispatchable_name_or_a_declared_form(
        filename: str) -> None:
    """The pin. Three names across the two files dispatched as nothing.

    Read raw rather than through the fixture: `builtin-ops` documents built-ins
    and never merges with a preset, and the example config is not this repo's
    own — loading its presets would answer about the wrong tree.
    """
    raw = json.loads((REPO_ROOT / filename).read_text(encoding="utf-8"))
    valid = set(supertool._valid_op_names())
    phantom = sorted(
        name for name, info in (raw.get("builtin-ops") or {}).items()
        if isinstance(info, dict) and name not in valid and not info.get("form")
    )
    assert not phantom, (
        f"{filename} names built-ins the dispatcher does not accept: "
        f"{phantom} — each one hands a machine enumerating the registry a "
        f"name that resolves to `unknown operation`"
    )


@pytest.mark.parametrize("filename", SHIPPED_CONFIGS)
def test_a_declared_form_in_either_shipped_config_names_a_real_parent(
        filename: str) -> None:
    raw = json.loads((REPO_ROOT / filename).read_text(encoding="utf-8"))
    valid = set(supertool._valid_op_names())
    bad = [f"{name}: form={info['form']!r}"
           for name, info in (raw.get("builtin-ops") or {}).items()
           if isinstance(info, dict) and info.get("form")
           and (info["form"] not in valid
                or not str(info.get("syntax", "")).startswith(info["form"]))]
    assert not bad, f"{filename}: {bad}"


def test_a_declared_form_names_a_parent_that_dispatches(
        shipped_config: dict) -> None:
    """`form` must not become the second place a name can be wrong. Same
    assertion as above, against the *merged* config the tool actually runs."""
    valid = set(supertool._valid_op_names())
    bad = []
    for name, info in (shipped_config.get("builtin-ops") or {}).items():
        if not isinstance(info, dict) or not info.get("form"):
            continue
        parent = info["form"]
        if parent not in valid:
            bad.append(f"{name}: form={parent!r} does not dispatch")
        elif not str(info.get("syntax", "")).startswith(parent):
            bad.append(f"{name}: syntax {info.get('syntax')!r} is not a "
                       f"{parent!r} invocation")
    assert not bad, bad


def test_a_form_entry_is_never_itself_a_dispatchable_name(
        shipped_config: dict) -> None:
    """The other direction: `form` must not be used to hide a real op."""
    valid = set(supertool._valid_op_names())
    hidden = sorted(
        name for name, info in (shipped_config.get("builtin-ops") or {}).items()
        if isinstance(info, dict) and info.get("form") and name in valid
    )
    assert not hidden, (
        f"declared as forms but really dispatchable: {hidden} — an op marked "
        f"this way drops out of every enumeration that trusts the key"
    )


def test_every_ops_entry_can_actually_run(shipped_config: dict) -> None:
    """A preset/project op resolves through `_resolve_custom_op`, which needs a
    `cmd`. An entry without one is a name in the registry that dispatches to
    nothing, the same defect one section over."""
    dead = sorted(name for name, info in (shipped_config.get("ops") or {}).items()
                  if not (isinstance(info, dict) and info.get("cmd")))
    assert not dead, f"ops entries with no cmd: {dead}"


def test_configured_op_names_does_not_report_a_form_as_an_op_name(
        shipped_config: dict) -> None:
    """`_configured_op_names` answers "which op names does this config have an
    opinion about". A form is not one, and this set is subtracted from the
    dispatcher's own names to decide what `ops` discloses as undocumented."""
    names = supertool._configured_op_names(shipped_config)
    forms = {n for n, i in (shipped_config.get("builtin-ops") or {}).items()
             if isinstance(i, dict) and i.get("form")}
    assert forms, "no form entries in the shipped config — this test is vacuous"
    assert not (forms & names), sorted(forms & names)


def test_the_two_documented_forms_are_still_documented(
        shipped_config: dict) -> None:
    """Deleting them would have been the cheaper fix and the wrong one: both
    carry `hint: true`, so their example is what survives `ops-compact`."""
    entries = shipped_config.get("builtin-ops") or {}
    for name, parent in (("read-grep", "read"), ("grep-count", "grep")):
        assert name in entries, f"{name} dropped from the registry"
        assert entries[name].get("form") == parent
        assert entries[name].get("hint") is True
    body = supertool.op_ops()
    assert "read:PATH:::grep=PATTERN" in body
    assert "count" in body


def test_a_form_entry_gets_no_at_file_route_of_its_own(
        shipped_config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adjacent, same mechanism, one surface over.

    `_build_at_file_registry` walks `builtin-ops` and, for any entry whose
    syntax carries `:::`, either registers a `@payload` route under that key or
    records it in `_AT_FILE_DROPPED_ROUTES` — the #770 diagnostic that answers
    "was this route dropped on purpose, or did a syntax edit delete it?".
    `read-grep`'s syntax is `read:PATH:::grep=PATTERN`, whose one derived field
    `grep=pattern` is not an identifier, so it landed in that list: a standing
    report of a discarded payload route belonging to a name that is not an op.
    Nothing reads the list at runtime, which is exactly why it went unnoticed.

    `read`'s own payload route is untouched here: it lives in
    `_READ_OP_AT_FIELDS`, deliberately outside this registry (#625).
    """
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY_BUILT", False)
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY", {})
    supertool._build_at_file_registry()
    assert supertool._at_file_specs("read-grep") == []
    assert "read-grep" not in dict(supertool._at_file_dropped_routes())
    assert "read" in supertool._READ_OP_AT_FIELDS


# --- `status` is a gate, not a label ---------------------------------------

def _op_listing(monkeypatch: pytest.MonkeyPatch, status: object) -> str:
    monkeypatch.setattr(supertool, "_CONFIG", {"ops": {
        "mystery": {"cmd": "true", "timeout": 5, "syntax": "mystery",
                    "safety": "read-only", "status": status}}})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    return supertool.op_ops()


def test_status_zero_removes_the_op_from_the_listing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`docs/contributing.md` called `status` informational. It is a truthy
    gate, and a falsy value makes an op that ships and dispatches unfindable —
    #1231's defect arriving through a second door."""
    assert "mystery" not in _op_listing(monkeypatch, 0)


def test_the_documented_status_values_do_not_hide_anything(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Both documented spellings are non-empty strings, hence truthy, hence
    informational *in effect* — which is why the doc's claim survived."""
    for value in ("experimental", "stable"):
        assert "mystery" in _op_listing(monkeypatch, value)


def test_contributing_does_not_call_status_informational_only() -> None:
    """The prose is the artefact that was wrong, so the prose is what is
    pinned. A row saying "Informational only" describes a key that cannot
    suppress an op, and this one can."""
    text = (REPO_ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")
    row = [ln for ln in text.splitlines() if ln.startswith("| `status`")]
    assert len(row) == 1, f"expected one `status` schema row, got {len(row)}"
    assert "Informational only" not in row[0], row[0]
    assert "0" in row[0], row[0]
