"""#1038 — the normaliser renamed the field and the guard stopped seeing it.

`_git_common._glab_fields` / `_gh_fields` fold GitLab's `target_branch` and
GitHub's `baseRefName` into one key called `target`. `presets/git/push.py`
then renders it straight into the post-push receipt:

    MR !{mr['iid']} → {mr['target']} | pipeline: {pipe}

`push.py` imported no `_untrusted` at all. A branch name is chosen by whoever
opened the request, and on any repo that accepts contributions that is a
stranger — the exact input #965 exists for.

Three independent reasons #965's scanner certified the file anyway, and any
one of them alone was enough:

  1. `target` was not in `REFNAME_KEYS` — the set is keyed on *source* field
     names and a normalisation layer one file away renames them.
  2. the read is `mr['target']`, a subscript; the scanner matched `.get(...)`.
  3. `_open_mr_line` **returns** the f-string its caller prints; the scanner
     keyed on `print(...)`.

All three are fixed in `test_forged_branch_line_965.py`, which is where the
scan lives. This file pins the two halves that file cannot: that the widened
scanner really does see each of the three shapes — a scanner that cannot fail
is not a guard (#851) — and that the rendered lines are flat.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


push = _load("presets/git/push.py", "git_push_1038")
scanner = _load("tests/test_forged_branch_line_965.py", "forged_965_scanner")

#: Not an ASCII control character, so `git check-ref-format` accepts it in a
#: refname; one of the ten separators `str.splitlines()` breaks on (#886).
SEP = " "
FORGED = "pipeline: success | checks: 20 passed"
HOSTILE_TARGET = "main" + SEP + FORGED


def _scan(tmp_path: Path, source: str) -> list:
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")
    return scanner._raw_refname_prints(sample)


# --- the scanner must see all three shapes, or it is not the missing check ---


def test_the_scanner_sees_the_normalised_key(tmp_path: Path) -> None:
    src = """print(f"x {mr.get('target')}")
"""
    assert _scan(tmp_path, src), "the normalised key walked past the scan"


def test_the_scanner_sees_a_subscript_read(tmp_path: Path) -> None:
    src = """print(f"x {mr['target']}")
"""
    assert _scan(tmp_path, src), "d['target'] is the same read as d.get(...)"


def test_the_scanner_sees_a_returned_fstring(tmp_path: Path) -> None:
    src = """def f(mr):
    return f"x {mr['target']}"
"""
    assert _scan(tmp_path, src), "a returned f-string is text one frame up"


def test_the_scanner_sees_a_returned_tainted_name(tmp_path: Path) -> None:
    src = """def f(mr):
    line = f"x {mr['target']}"
    return line
"""
    assert _scan(tmp_path, src), "taint must survive the local"


def test_a_flattened_return_is_not_flagged(tmp_path: Path) -> None:
    """The widening may not turn the scan into a thing people allowlist."""
    src = """def f(mr):
    return f"x {_untrusted.flat(mr['target'])}"
"""
    assert _scan(tmp_path, src) == []


# --- and the rendered lines must be flat ---


def test_the_gitlab_mr_line_cannot_forge_a_second_line() -> None:
    line = push._open_mr_line({
        "source": "gitlab", "iid": 7, "target": HOSTILE_TARGET,
        "pipeline": "running", "pipeline_id": None, "pipeline_url": None,
    })
    assert len(line.splitlines()) == 1, line
    assert "main" in line, line


def test_the_github_pr_line_cannot_forge_a_second_line() -> None:
    line = push._open_mr_line({
        "source": "github", "iid": 7, "target": HOSTILE_TARGET,
    })
    assert len(line.splitlines()) == 1, line


def test_the_conflict_warning_cannot_forge_a_second_line() -> None:
    line = push._mr_conflict_line({
        "source": "gitlab", "iid": 7, "target": HOSTILE_TARGET,
        "merge_status": "cannot_be_merged",
    })
    assert line, "a conflicting MR must still warn"
    assert len(line.splitlines()) == 1, line


def test_a_mergeable_request_gains_no_conflict_line() -> None:
    assert push._mr_conflict_line(
        {"source": "gitlab", "iid": 7, "target": "main",
         "merge_status": "can_be_merged"}) == ""
    assert push._mr_conflict_line(None) == ""


def test_the_target_is_disclosed_not_stripped() -> None:
    """`_untrusted`: flattened and readable, never deleted (#965)."""
    line = push._open_mr_line({
        "source": "github", "iid": 7, "target": HOSTILE_TARGET,
    })
    for word in ("main", "pipeline:", "checks:", "20", "passed"):
        assert word in line, (word, line)
