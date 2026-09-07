"""#938 -- `replace` shares the silent double-apply `edit` was fixed for in #701.

Confirmed live in the issue's own follow-up comment (2026-08-13): `edit`
(and therefore `batch:@payload` with `op="edit"`, which routes through the
same `op_edit` via `dispatch(pre_parsed=...)`) already discloses a re-run
whose `new` contains its own `old` -- see test_reapply_disclosure_701.py.
`replace` did not: two runs of the identical payload printed byte-identical
receipts while the file gained a second copy of the inserted text -- the
exact residual the issue's own re-scoping comment names and leaves open.

Every "must disclose" case below is paired with a "must still allow a
genuinely-repeated edit" case, per this project's own named defect class (an
absence produced by the tool read as an absence in the world) -- a check that
only ever asserts silence passes just as well when the detection code never
ran at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool


def _result_line_of(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("[result] "):
            return line
    return ""


def _payload(tmp_path: Path, target: Path, old: str, new: str) -> Path:
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "replace", "path": str(target), "old": old, "new": new},
    ]), encoding="utf-8")
    return payload


def test_second_replace_run_is_disclosed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "mod.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    old = "def f():"
    new = "@decorated" + chr(10) + "def f():"
    payload = _payload(tmp_path, f, old, new)

    first = supertool.dispatch(f"batch:@{payload}")
    second = supertool.dispatch(f"batch:@{payload}")

    assert "re-applied" not in first
    assert _result_line_of(first) == "[result] 1 op run, 1 write"
    assert "re-applied" in second
    assert _result_line_of(second) != _result_line_of(first)


def test_second_replace_run_still_writes(tmp_path: Path, monkeypatch) -> None:
    """Disclosure, not refusal -- same posture #701 took for `edit`. A
    genuinely-repeated edit (two decorators wanted) must still land."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "mod.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    old = "def f():"
    new = "@decorated" + chr(10) + "def f():"
    payload = _payload(tmp_path, f, old, new)

    supertool.dispatch(f"batch:@{payload}")
    supertool.dispatch(f"batch:@{payload}")

    assert f.read_text(encoding="utf-8") == (
        "@decorated\n@decorated\ndef f():\n    return 1\n"
    )


def test_first_replace_run_says_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "mod.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    old = "def f():\n    return 1"
    new = "def f():\n    return 2"
    payload = _payload(tmp_path, f, old, new)
    out = supertool.dispatch(f"batch:@{payload}")
    assert "re-applied" not in out
    assert _result_line_of(out) == "[result] 1 op run, 1 write"


def test_new_text_existing_elsewhere_is_not_a_re_application(
    tmp_path: Path, monkeypatch
) -> None:
    """The naive `new in content` test's failure mode, over `replace` this
    time: `new` pre-exists in an unrelated function, and this is a genuine
    first application at a different site."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "mod.py"
    f.write_text(
        "def g():\n    return None\n\ndef h():\n    return 1\n", encoding="utf-8"
    )
    payload = _payload(tmp_path, f, "    return 1", "    return None")
    out = supertool.dispatch(f"batch:@{payload}")
    assert "re-applied" not in out
    assert _result_line_of(out) == "[result] 1 op run, 1 write"


def test_multiple_occurrences_per_file_first_run_says_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """`replace` is replace-all, unlike `edit` (which refuses more than one
    occurrence as ambiguous before this code ever runs). A first application
    over a file with THREE independent occurrences of `old` must not flag any
    of them, and must count all three: the multi-occurrence-per-file shape
    where `edit`'s single-occurrence assumption cannot be carried over."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "mod.py"
    f.write_text(
        "def a():\n    pass\n\ndef a():\n    pass\n\ndef a():\n    pass\n",
        encoding="utf-8",
    )
    payload = _payload(tmp_path, f, "def a():\n    pass",
                       "def a():\n    pass  # touched")
    out = supertool.dispatch(f"batch:@{payload}")
    assert "re-applied" not in out
    assert _result_line_of(out) == "[result] 1 op run, 1 write"
    assert f.read_text(encoding="utf-8").count("# touched") == 3


def test_multiple_occurrences_per_file_second_run_counts_every_reapply(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-run the same multi-occurrence payload. Every one of the three
    occurrences is now sitting inside its own prior application, so all
    three must be counted -- not just the first one found, and not zero."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "mod.py"
    f.write_text(
        "def a():\n    pass\n\ndef a():\n    pass\n\ndef a():\n    pass\n",
        encoding="utf-8",
    )
    payload = _payload(tmp_path, f, "def a():\n    pass",
                       "def a():\n    pass  # touched")

    supertool.dispatch(f"batch:@{payload}")
    second = supertool.dispatch(f"batch:@{payload}")

    assert "[3 re-applied]" in second
    line = _result_line_of(second)
    assert line.startswith("[result] 1 op run, 1 write, 3 re-applied")
    assert "an edit already present in the file was applied again" in line
    assert f.read_text(encoding="utf-8").count("# touched  # touched") == 3
