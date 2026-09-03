"""2081 — a preset-supplied `builtin-ops.<op>.syntax` overrides `help:OP`'s
rendered payload keys for a read-family op, but dispatch for that op never
consults the registry `syntax` drives: `_read_op_from_payload` looks up
`_READ_OP_AT_FIELDS[op]` directly (#625's deliberate split, see the comment
above that dict). So a manifest can make `help:read` advertise `where` /
`howmuch` while `read:@-` still only accepts `path`, `offset`, `limit`,
`grep`, `full` — the documented keys are refused.

Not a crash and not data loss (#2081's own severity note): the refusal is
honest and names the real keys, so the cost is one wasted round-trip. Still a
misreport, and this pins the fix: an op declared in `_READ_OP_AT_FIELDS`
keeps rendering from that dict regardless of what a config `builtin-ops`
entry's `syntax` says, because dispatch for that op was never wired to read
`syntax` in the first place.
"""
from __future__ import annotations

import pytest

import supertool

MARKER = "Payload route"


@pytest.fixture(autouse=True)
def _fake_config_with_evil_syntax(monkeypatch):
    """A `builtin-ops.read` entry supplying a `:::`-syntax that does not match
    the real dispatch fields — the shape #2081 reproduced with
    `presets/evil2.json`."""
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG", {
        "builtin-ops": {
            "read": {"syntax": "read:::WHERE:::HOWMUCH"},
        },
    })
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY", {})
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY_BUILT", False)


def test_help_read_still_names_the_keys_dispatch_actually_accepts() -> None:
    out = supertool.op_help("read")
    assert MARKER in out
    for field in supertool._READ_OP_AT_FIELDS["read"]:
        assert field in out, f"help:read did not name {field!r}"
    # The bogus keys from the manifest's own syntax string must not leak in —
    # that is the exact misreport #2081 filed.
    assert "where" not in out
    assert "howmuch" not in out


def test_read_at_file_specs_are_not_overridden_by_config_syntax() -> None:
    """`_at_file_specs("read")` is not where `read`'s route lives (#625, see
    `_READ_OP_AT_FIELDS`'s own comment) — it stays empty regardless of the
    config, which is exactly what stops a manifest `syntax` from overlaying
    bogus fields onto it. `help:` falls back to `_READ_OP_AT_FIELDS` when
    this is empty (see `_documented_route_ops` in test_help_payload_route_1400),
    which is the fallback this fix relies on rather than bypasses."""
    assert supertool._at_file_fields("read") == []
    assert not any(f in ("where", "howmuch")
                    for f in supertool._at_file_fields("read"))
