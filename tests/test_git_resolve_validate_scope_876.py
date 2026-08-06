"""The post-resolve syntax digest must mean exactly what it says (#876, #878).

The digest shells back into supertool. It used to build the colon list form

    validate:f1,f2,…:FILTER

with `','.join(files)`. Those separators are characters a filename may legally
contain, so a conflicted file called `x:ruff` re-parsed the op: the field the
receiver read as the validator filter was no longer the field this module
chose. Argv is list-form, so there was no shell exposure and no new process —
the harm is scope, a locally-configured validator running over a set nobody
selected.

The first fix filtered such paths out of the batch, and the shape of why that
failed is what most of this file is about. The receiver's tokenizer already
reassembles a Windows drive letter, per comma-segment, exactly so the list form
can carry `D:\\a\\x.php,D:\\a\\y.php`. A sender-side `":" not in p` is a second,
cruder copy of that rule, written at the wrong end of the pipe and missing its
only interesting case — so it excluded precisely the paths the receiver handles
best, on the one platform where every absolute path contains a colon, and it
excluded them *silently*: `_validate_paths` returned all-`None`, the caller's
`if digest:` printed nothing, and the receipt read `markers: clean` —
indistinguishable from a check that ran and passed. A loud, exotic bug traded
for a quiet, universal one.

So the guard is at the receiver's parse, not the sender's input. Re-deriving
the tokenizer here would have to stay in sync with it forever, and every future
caller would have to re-derive it too; the payload route instead carries the
file list in a field that is never tokenized. The post-condition pinned here is
about the *receiver's reading*, not about a sanitiser being called — **whatever
leaves this module must re-parse to exactly the files and exactly the filter
the module intended, and every path must survive the trip on every platform.**

Nothing here writes an awkward filename to disk. `x:ruff` is not creatable on
Windows and `D:\\a\\repo\\x.py` is not creatable anywhere else, and the property
under test is what the sender transmits and the receiver reads — not what the
filesystem will accept. Stubbing `isfile` tests the same property on all four
legs instead of on whichever one happens to tolerate the fixture.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import supertool

import importlib.util


ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_876", PRESET)
assert _spec is not None and _spec.loader is not None
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


#: Paths the sender must transmit intact. The first two are legal POSIX
#: filenames the colon form genuinely cannot carry; the third is an ordinary
#: Windows absolute path that it carries fine — and that the sender-side filter
#: could not tell apart from the other two. All three are the same requirement
#: on the payload: transmit what you were given.
AWKWARD_PATHS = [
    "x:ruff",
    "a,b.py",
    r"D:\a\claude-supertool\claude-supertool\tests\test_git_resolve.py",
]


class _Captured:
    """Record the op string and the payload the digest actually transmits."""

    def __init__(self) -> None:
        self.ops: list[str] = []
        self.payloads: list[dict] = []

    def run(self, argv, **kw):
        self.ops.append(argv[-1])
        self.payloads.append(json.loads(kw["input"]))
        return subprocess.CompletedProcess(argv, 0, stdout="no validators", stderr="")


def _capture(monkeypatch) -> _Captured:
    cap = _Captured()
    monkeypatch.setattr(rs.subprocess, "run", cap.run)
    monkeypatch.setattr(rs.os.path, "isfile", lambda p: True)
    return cap


# ---------------------------------------------------------------------------
# Sender: what leaves the module
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("awkward", AWKWARD_PATHS)
def test_a_separator_in_a_path_cannot_repoint_the_validator_filter(monkeypatch, awkward) -> None:
    """`x:ruff` must not become the filter, and must not be dropped either."""
    cap = _capture(monkeypatch)
    rs._validate_paths([awkward, "a.py"])

    assert cap.ops, "the digest call was never made"
    for op, payload in zip(cap.ops, cap.payloads):
        # No fields on the colon CLI at all — nothing left for a path to steal.
        assert op == "validate:@-", op
        assert payload["tools"] in (rs._SYNTAX_FILTER, list(rs._SYNTAX_VALIDATORS)), payload
        assert payload["paths"] == [awkward, "a.py"], payload


@pytest.mark.parametrize("awkward", AWKWARD_PATHS)
def test_a_separator_in_a_path_cannot_widen_the_file_list(monkeypatch, awkward) -> None:
    """One file must re-parse as one file, whatever characters are in its name."""
    cap = _capture(monkeypatch)
    rs._validate_paths([awkward])

    assert cap.payloads, "the digest never ran — the file was excluded"
    for payload in cap.payloads:
        assert payload["paths"] == [awkward], payload


def test_a_windows_absolute_path_is_still_digested(monkeypatch) -> None:
    """The regression that took all four Windows legs red.

    `tmp_path` and `__file__` are absolute, so on Windows every path the digest
    is handed carries a drive-letter colon — a path the receiver has always
    parsed correctly. Excluding it is not a narrower batch; it is no batch at
    all, on that platform, forever.
    """
    cap = _capture(monkeypatch)
    win = r"D:\a\claude-supertool\claude-supertool\tests\test_git_resolve.py"
    digests = rs._validate_paths([win])

    assert len(cap.ops) == 1, "the digest never ran — the file was excluded"
    assert cap.payloads[0]["paths"] == [win]
    assert set(digests) == {win}


def test_ordinary_paths_are_still_digested(monkeypatch, tmp_path) -> None:
    """The guard must not cost the feature: normal files still go in the batch."""
    a = tmp_path / "a.py"
    a.write_text("x", encoding="utf-8")
    b = tmp_path / "b.json"
    b.write_text("{}", encoding="utf-8")

    cap = _capture(monkeypatch)
    rs._validate_paths([str(a), str(b)])

    assert cap.payloads[0]["paths"] == [str(a), str(b)], cap.payloads[0]


# ---------------------------------------------------------------------------
# Receiver: what it reads back
# ---------------------------------------------------------------------------

def test_the_receiver_reads_exactly_the_paths_the_payload_named(monkeypatch) -> None:
    """The other half of the post-condition, asserted at the parse itself."""
    seen: dict[str, object] = {}

    def recorder(paths, tool_filter=None, verbose=False):
        seen["paths"] = paths
        seen["tools"] = tool_filter
        return ""

    monkeypatch.setattr(supertool, "op_validate_multi", recorder)
    monkeypatch.setattr(supertool, "_safe_path", lambda p: p)
    supertool._read_op_from_payload(
        "validate", {"paths": list(AWKWARD_PATHS), "tools": "@syntax"}
    )

    assert seen["paths"] == AWKWARD_PATHS
    assert seen["tools"] == ["@syntax"]


def test_the_receiver_still_reads_a_single_path_as_the_single_form(monkeypatch) -> None:
    """`path` keeps the singular spelling every other read op uses."""
    seen: dict[str, object] = {}

    def recorder(path, tool_filter=None, verbose=False):
        seen["path"] = path
        seen["tools"] = tool_filter
        return ""

    monkeypatch.setattr(supertool, "op_validate", recorder)
    supertool._read_op_from_payload("validate", {"path": "x:ruff", "tools": "phplint,xmllint"})

    assert seen["path"] == "x:ruff"
    assert seen["tools"] == ["phplint", "xmllint"]


def test_the_receiver_names_the_missing_field_rather_than_validating_nothing() -> None:
    out = supertool._read_op_from_payload("validate", {"tools": "@syntax"})
    assert "ERROR" in out and "'path'" in out


# ---------------------------------------------------------------------------
# The silence this fix must not reintroduce
# ---------------------------------------------------------------------------

def test_a_check_that_could_not_run_is_reported_not_dropped(monkeypatch) -> None:
    """`None` means "ran, nothing matched". It must not also mean "never ran".

    The caller prints the digest only `if digest`, so `None` renders as nothing
    at all under a `markers: clean` line — the exact silence #880 is about. A
    check that could not run therefore returns a line instead.
    """
    def timing_out(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 90)

    monkeypatch.setattr(rs.subprocess, "run", timing_out)
    monkeypatch.setattr(rs.os.path, "isfile", lambda p: True)
    digests = rs._validate_paths(["x:ruff"])

    assert digests["x:ruff"], "an undigested path rendered as nothing at all"
    assert "not checked" in digests["x:ruff"]


def test_a_path_that_is_not_a_file_is_reported_not_dropped(monkeypatch) -> None:
    cap = _Captured()
    monkeypatch.setattr(rs.subprocess, "run", cap.run)
    monkeypatch.setattr(rs.os.path, "isfile", lambda p: False)
    digests = rs._validate_paths(["gone.php"])

    assert not cap.ops, "nothing to validate, so nothing should have been shelled"
    assert digests["gone.php"] and "not checked" in digests["gone.php"]


def test_no_matching_validator_is_still_a_quiet_none(monkeypatch) -> None:
    """The opposite guard: a real "nothing handles this type" must stay quiet.

    Otherwise every resolved `.txt` and `.md` grows a warning line, and a
    receipt that warns about everything warns about nothing.
    """
    def answered(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="no validators configured\n", stderr="")

    monkeypatch.setattr(rs.subprocess, "run", answered)
    monkeypatch.setattr(rs.os.path, "isfile", lambda p: True)
    assert rs._validate_paths(["notes.txt"])["notes.txt"] is None
