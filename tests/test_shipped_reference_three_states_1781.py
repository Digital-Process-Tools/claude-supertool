"""#1781 — three worlds printed one sentence, and the sentence made a claim.

`_shipped_config()` (#1773) catches `OSError` and yields `{}`, so the refusal
built on it read:

    ERROR: op 'read' has no documented help here, and none shipped with this
    binary either.

The v0.47.0 release audit built three installs from this checkout's own
`supertool.py` / `_supertool.py` and ran each from an empty cwd:

    C  .supertool.json absent                    -> the refusal above
    B  .supertool.json present, malformed JSON   -> the refusal above
    D  .supertool.json present, chmod 000        -> the refusal above

`cmp -s` reported D and C byte-for-byte identical, and `json.load` on D's file
after `chmod 644` confirmed `read` **is** documented in it. So the sentence was
false in D and unverified in B: the binary shipped the reference and could not
read it.

That is this repository's own defect class landing inside the fix for it — an
absence produced by the tool, read as an absence in the world. The fix is the
shape `docs/validators.md` already prescribes: **three states, not two.** The
lookup records whether the file was read, was absent, or could not be read, and
the refusal says which. A reader told the reference does not document their op
can act on it; a reader told it could not be read knows the answer is UNKNOWN
and that the remedy is a file permission, not a bug report.

The second finding is in the same family and is asserted here too: the `ops`
footer named `ops:full`'s whole render as "the description of every op above",
overstating the withheld prose by the size of the default listing itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def consumer_tree(monkeypatch: pytest.MonkeyPatch):
    """A config that documents nothing — the shape every consumer install has."""
    monkeypatch.setattr(supertool, "_CONFIG", {})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", "/somewhere/else/.supertool.json")


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents):
    """Point the shipped lookup at `tmp_path`, optionally writing a config.

    `contents=None` leaves the directory empty (install C). A string is written
    verbatim, so a malformed one is install B. The caller chmods for install D.
    """
    monkeypatch.setattr(supertool, "_SHIPPED_CONFIG", None)
    monkeypatch.setattr(supertool, "_SHIPPED_CONFIG_DIR", str(tmp_path))
    path = tmp_path / ".supertool.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    return path


def test_a_readable_reference_that_documents_nothing_says_so(
        tmp_path: Path, monkeypatch, consumer_tree) -> None:
    """The one state where "it does not document this op" is a true sentence."""
    _install(tmp_path, monkeypatch, json.dumps({"builtin-ops": {}}))
    out = supertool.op_help("read")
    assert out.startswith("ERROR")
    assert "UNKNOWN" not in out
    assert "does not document" in out


def test_an_absent_reference_is_named_as_absent(
        tmp_path: Path, monkeypatch, consumer_tree) -> None:
    """Install C. The remedy is a broken install, and the line may say so."""
    _install(tmp_path, monkeypatch, None)
    out = supertool.op_help("read")
    assert out.startswith("ERROR")
    assert "not there" in out or "absent" in out
    assert "UNKNOWN" not in out


@pytest.mark.parametrize("contents", ["{not json", '["a list, not an object"]'])
def test_an_unreadable_reference_declines_rather_than_reporting_none(
        tmp_path: Path, monkeypatch, consumer_tree, contents: str) -> None:
    """Install B. Whether the op is documented was never established, so the
    refusal must not answer it — the whole finding is that this rendered
    identically to install C."""
    _install(tmp_path, monkeypatch, contents)
    out = supertool.op_help("read")
    assert out.startswith("ERROR")
    assert "UNKNOWN" in out
    assert "does not document" not in out


def test_a_present_but_unreadable_reference_is_not_an_absent_one(
        tmp_path: Path, monkeypatch, consumer_tree) -> None:
    """Install D, the specimen: a complete reference the process cannot open.

    Skipped where the chmod does not bite — root ignores the mode bits, and
    Windows does not implement them this way. A test that cannot create the
    state must not pass as though it checked it.
    """
    path = _install(tmp_path, monkeypatch,
                    json.dumps({"builtin-ops": {"read": {"syntax": "read:PATH"}}}))
    path.chmod(0o000)
    try:
        try:
            path.read_text(encoding="utf-8")
        except OSError:
            pass
        else:
            pytest.skip("this process can read a 0o000 file; the state cannot "
                        "be built here")
        out = supertool.op_help("read")
    finally:
        path.chmod(0o644)
    assert out.startswith("ERROR")
    assert "UNKNOWN" in out
    assert "does not document" not in out


def test_the_three_refusals_are_three_different_strings(
        tmp_path: Path, monkeypatch, consumer_tree) -> None:
    """The finding restated as an assertion: `cmp -s` reported two of these
    byte-for-byte identical."""
    seen = []
    for contents in (json.dumps({"builtin-ops": {}}), None, "{not json"):
        d = tmp_path / f"install-{len(seen)}"
        d.mkdir()
        _install(d, monkeypatch, contents)
        seen.append(supertool.op_help("read"))
    assert len(set(seen)) == 3, seen


def test_the_state_is_reset_between_lookups(tmp_path: Path, monkeypatch,
                                            consumer_tree) -> None:
    """A cached verdict from one install must not describe the next one."""
    _install(tmp_path, monkeypatch, "{not json")
    assert "UNKNOWN" in supertool.op_help("read")
    other = tmp_path / "second"
    other.mkdir()
    _install(other, monkeypatch, json.dumps({"builtin-ops": {}}))
    assert "UNKNOWN" not in supertool.op_help("read")


def test_the_footer_number_is_what_ops_full_costs(shipped_config) -> None:
    """Measured: `ops` 3,676 and `ops:full` 74,838, so naming the larger number
    as "the description of every op" overstated the prose by the whole default
    render. The number stays — it is what a caller spends — and the sentence
    now says which quantity it is."""
    body = supertool.op_ops()
    full = len(supertool.op_ops(full=True).encode("utf-8"))
    assert str(full) in body
    assert "ops:full" in body
    footer = body[body.index("Signatures only"):]
    assert "description of every op above is withheld" not in footer
