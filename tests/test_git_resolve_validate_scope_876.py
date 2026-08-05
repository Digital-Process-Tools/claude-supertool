"""`git-resolve` builds its validate call out of filenames (#876).

The post-resolve syntax digest shells back into supertool with the list form

    validate:f1,f2,…:FILTER

built by `','.join(files)`. The separators are the same characters a filename
may legally contain, so a conflicted file called `x:ruff` re-parses the op:
the field the receiver reads as the validator filter is no longer the field
this module chose. Argv is list-form, so there is no shell exposure and no new
process — the harm is scope, a locally-configured validator running over a set
nobody selected.

Same defect as the worktrees board: a filename interpolated into a structured
string that has no escape for it. The post-condition pinned here is about the
*receiver's* reading, not about a sanitiser being called — **whatever op string
leaves this module must re-parse to exactly the files and exactly the filter
the module intended.**
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_876", PRESET)
assert _spec is not None and _spec.loader is not None
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


class _Captured:
    def __init__(self) -> None:
        self.ops: list[str] = []

    def run(self, argv, **kw):
        self.ops.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout="no validators", stderr="")


def _capture(monkeypatch) -> _Captured:
    cap = _Captured()
    monkeypatch.setattr(rs.subprocess, "run", cap.run)
    return cap


def _reparse(op: str) -> tuple[list[str], str]:
    """Read the op string the way the receiving CLI reads it: split on ':'."""
    parts = op.split(":")
    assert parts[0] == "validate", op
    return parts[1].split(","), ":".join(parts[2:])


def test_a_colon_in_a_filename_cannot_repoint_the_validator_filter(monkeypatch, tmp_path) -> None:
    """`x:ruff` must not become the filter the receiver applies."""
    hostile = tmp_path / "x:ruff"
    hostile.write_text("x", encoding="utf-8")
    benign = tmp_path / "a.py"
    benign.write_text("x", encoding="utf-8")

    cap = _capture(monkeypatch)
    rs._validate_paths([str(hostile), str(benign)])

    assert cap.ops, "the digest call was never made"
    for op in cap.ops:
        _files, tool_filter = _reparse(op)
        assert tool_filter in (rs._SYNTAX_FILTER, ",".join(rs._SYNTAX_VALIDATORS)), op


def test_a_comma_in_a_filename_cannot_widen_the_file_list(monkeypatch, tmp_path) -> None:
    """One file must not re-parse into two paths, neither of them real."""
    hostile = tmp_path / "a,b.py"
    hostile.write_text("x", encoding="utf-8")

    cap = _capture(monkeypatch)
    rs._validate_paths([str(hostile)])

    for op in cap.ops:
        files, _filter = _reparse(op)
        for name in files:
            assert Path(name).is_file(), f"{name!r} is not a real path — {op}"


def test_the_undigestible_file_is_reported_as_undigested_not_as_clean(monkeypatch, tmp_path) -> None:
    """Declining is fine; a silent `ok` for a file nobody validated is not.

    The digest is advisory and `None` is its documented "did not answer", so a
    file whose name cannot be carried through the op string must land there —
    the same rule the worktrees op applies to `cannot tell`.
    """
    hostile = tmp_path / "x:ruff"
    hostile.write_text("x", encoding="utf-8")

    _capture(monkeypatch)
    digests = rs._validate_paths([str(hostile)])
    assert digests[str(hostile)] is None, digests


def test_ordinary_paths_are_still_digested(monkeypatch, tmp_path) -> None:
    """The guard must not cost the feature: normal files still go in the batch."""
    a = tmp_path / "a.py"
    a.write_text("x", encoding="utf-8")
    b = tmp_path / "b.json"
    b.write_text("{}", encoding="utf-8")

    cap = _capture(monkeypatch)
    rs._validate_paths([str(a), str(b)])

    files, _filter = _reparse(cap.ops[0])
    assert files == [str(a), str(b)], cap.ops[0]
