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
import subprocess
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
#:
#: Spelled as an escape, not as a literal character. The first draft of this
#: file carried the literal, it did not survive authoring, and `SEP` was a
#: plain U+0020 — which made every `len(line.splitlines()) == 1` assertion
#: below trivially true and the whole file vacuous. A test that passes when
#: the code does nothing is the defect this repo is named for, arriving in
#: the test written to catch it. `test_sep_is_a_real_line_separator` pins it.
SEP = chr(0x2028)
FORGED = "pipeline: success | checks: 20 passed"
HOSTILE_TARGET = "main" + SEP + FORGED


def test_sep_is_a_real_line_separator() -> None:
    """Guard the guard: every forgery assertion below is built on this.

    `SEP` was a plain space for one draft of this file. `len(...splitlines())
    == 1` is then true of the hostile value itself, so each of those tests
    passed against completely unflattened code. The character is built with
    `chr()` rather than written literally because the literal is invisible in
    a diff and did not survive being typed once already.
    """
    assert len(HOSTILE_TARGET.splitlines()) == 2, repr(HOSTILE_TARGET)
    assert SEP not in _untrusted_flat(HOSTILE_TARGET), (
        "flat() must remove the separator, or these tests prove nothing")


def _untrusted_flat(text: str) -> str:
    return _load("presets/_untrusted.py", "untrusted_1038").flat(text)


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

# --- review of PR #1092 found two more sinks for the same value ---------------

pr_merge = _load("presets/github/pr_merge.py", "gh_pr_merge_1038")


def _fake_git(out: str, rc: int = 0) -> Any:
    seen = []

    def run(args: list, timeout: Any = None) -> Any:
        seen.append(list(args))
        return subprocess.CompletedProcess(
            args=["git"] + list(args), returncode=rc, stdout=out, stderr="")
    run.seen = seen
    return run


def test_the_stale_base_advisory_cannot_forge_a_line(
        monkeypatch: Any, capsys: Any) -> None:
    """The third consumer of `target`, one call-hop from the two that were fixed.

    `_post_push_advisories` reads `mr['target']` and hands it to
    `_stale_base_advisory`, which builds `ref = f"{remote}/{target}"` and
    prints it at column 0 in four places. The scanner cannot see this: the
    value leaves as a call argument, not a print or a return, and there is no
    interprocedural taint tracking. So #1038's own guard went green over it.
    """
    monkeypatch.setattr(push, "_git", _fake_git("3" + chr(10)))
    push._stale_base_advisory(HOSTILE_TARGET, "origin")
    out = capsys.readouterr().out
    forged = [ln for ln in out.splitlines() if ln.lstrip().startswith(FORGED)]
    assert not forged, out
    assert "main" in out, out


def test_the_stale_base_check_measures_the_unflattened_ref(
        monkeypatch: Any) -> None:
    """Flatten the echo, never the value the check is computed from.

    Flattening on the way *in* would change which ref `rev-list` counts
    against, turning a loud forgery into a quiet wrong answer about how stale
    the base is — the trade docs/validators.md refuses ("Validators still run
    against the real, unflattened path — the flattening is on the echo only").
    """
    fake = _fake_git("0" + chr(10))
    monkeypatch.setattr(push, "_git", fake)
    push._stale_base_advisory(HOSTILE_TARGET, "origin")
    assert fake.seen, "the check never ran"
    assert any(HOSTILE_TARGET in part
               for args in fake.seen for part in args), fake.seen


def test_the_skipped_arm_flattens_too(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(push, "_git", _fake_git("", rc=128))
    push._stale_base_advisory(HOSTILE_TARGET, "origin")
    out = capsys.readouterr().out
    forged = [ln for ln in out.splitlines() if ln.lstrip().startswith(FORGED)]
    assert not forged, out


def test_zero_check_runs_offers_no_op_for_an_unordinary_branch() -> None:
    """A convenience command is declined, not flattened (`presets/_refname.py`).

    `pr_merge` already draws this line for the head-branch *delete* command:
    flattening fixes the render and changes the ref, so the printed command
    names a branch that is not this one. The `gh-branch:` pointer in the
    zero-checks refusal is the same convenience case and was flattened into
    exactly that safe-but-wrong shape.
    """
    lines = pr_merge._check_findings(
        {"number": 7, "headRefOid": "a" * 40, "statusCheckRollup": [],
         "headRefName": HOSTILE_TARGET},
        None, [])
    joined = chr(10).join(lines)
    # Not `"gh-branch:" not in joined` — the decline text names the op it is
    # declining to offer, which is the point of it. What must be absent is an
    # invocation: the op token followed by the branch it would act on.
    assert "gh-branch:main" not in joined, joined
    assert "no `gh-branch:` command is offered" in joined, joined
    # Declined, not censored: the name is still readable in full (#965).
    assert "main" in joined, joined
    assert "20 passed" in joined, joined
    for line in joined.splitlines():
        assert not line.lstrip().startswith(FORGED), joined


def test_zero_check_runs_still_offers_the_op_for_an_ordinary_branch() -> None:
    """The common case may not lose the pointer it had."""
    lines = pr_merge._check_findings(
        {"number": 7, "headRefOid": "a" * 40, "statusCheckRollup": [],
         "headRefName": "fix/993"},
        None, [])
    joined = chr(10).join(lines)
    assert "gh-branch:fix/993" in joined, joined


def test_zero_check_runs_declines_the_op_for_a_colon_carrying_branch() -> None:
    """#1089's own live-instance claim, checked directly.

    A `:` in a branch name is well inside what `check-ref-format` accepts
    and is exactly the byte supertool's own colon CLI splits an op string
    on -- `gh-branch:feature:x` would not name the branch `feature:x`, it
    would name a THIRD op-string field. #1092 already gated this exact
    pointer on `_refname.ordinary()`, which excludes `:`
    (`presets/_refname.py`'s `ORDINARY_REF` character class has no colon in
    it); this pins that a colon-carrying head branch takes the SAME declined
    path as the U+2028 case above, not a variant of it.
    """
    lines = pr_merge._check_findings(
        {"number": 7, "headRefOid": "a" * 40, "statusCheckRollup": [],
         "headRefName": "feature:x"},
        None, [])
    joined = chr(10).join(lines)
    assert "gh-branch:feature:x" not in joined, joined
    assert "gh-branch:feature" not in joined, joined
    assert "no `gh-branch:` command is offered" in joined, joined
    # Declined, not censored (#965): the name is still readable in full.
    assert "feature:x" in joined, joined
