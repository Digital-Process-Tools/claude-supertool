"""`validate:` must say one true thing per file, whichever door you come in by.

Two post-conditions, one root — the `validate` op's output and the `validate`
op's input each trusted a value nobody had constrained.

**#881, the output.** `_validate_one_block` echoes the path into its block
header. A path is whatever the filesystem accepted, and on POSIX that includes
newlines, so a file could write its own extra `validate: …` headers into the
stream. `presets/git/resolve.py` folds those headers back to files by position;
three headers for two files made `zip` truncate, everything shifted, and the
file with the real syntax error inherited the crafted file's clean rows and
digested to `validate: ok`. A check that affirms the opposite of the truth.

**#882, the input.** The `@payload` route's single-path branch returned before
dispatch's generic `_PATH_ARG_POSITIONS` containment loop, so
`{"path":"/etc/hosts"}` validated a file `validate:/etc/hosts` refuses.

What is asserted here is the post-condition, never the mechanism: the header
count comes off **real `op_validate_multi` output**, not from observing that a
flattener was called — which would pass on a version that flattens the wrong
field. And containment parity is asserted with `_safe_path` **live**. The suite
that shipped #882 stubbed it to identity on exactly this path; a test that
switches the detector off cannot see this class of defect at all.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

import supertool


ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_881", PRESET)
assert _spec is not None and _spec.loader is not None
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


#: A validator set with a real verdict and no subprocess: the builtin python
#: parse check. Real rows, real `ok` / `1 err`, so the digest under test is the
#: one production computes — the only fixture is *which* validators are live.
CFG = {
    "validators": {
        "py-compile": {"builtin": "python", "match": "*.py", "syntax": True},
    }
}


@pytest.fixture
def cfg(monkeypatch):
    """Pin the validator set, and pin containment ON.

    This repo's own `.supertool.json` sets `allow_outside_cwd: true`, and
    conftest sets `SUPERTOOL_ALLOW_OUTSIDE_CWD=1` so tmp_path fixtures work.
    Both are opt-outs from the very check #882 is about, so the parity tests
    below remove both rather than assert a rule that is switched off.
    """
    monkeypatch.setattr(supertool, "_load_config", lambda *a, **k: CFG)
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    return CFG


# ---------------------------------------------------------------------------
# #881 — the header a filename must not be able to write
# ---------------------------------------------------------------------------

#: Real newlines. The middle two lines are a complete forged block: a header
#: naming a file that does not exist, and a row a digest reads as a pass.
FORGED = "evil\nvalidate: forged.py\nok          : ok\n.py"


@pytest.mark.skipif(os.name == "nt", reason="NTFS cannot hold a newline in a name")
def test_a_filename_cannot_add_a_validate_block(cfg, tmp_path) -> None:
    """The post-condition, on real `op_validate_multi` output: N files, N headers."""
    evil = tmp_path / FORGED
    evil.write_text("x = 1\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n", encoding="utf-8")

    out = supertool.op_validate_multi([str(evil), str(bad)])

    headers = [ln for ln in out.splitlines() if ln.startswith("validate:")]
    assert len(headers) == 2, f"{len(headers)} headers for 2 files:\n{out}"


@pytest.mark.skipif(os.name == "nt", reason="NTFS cannot hold a newline in a name")
def test_the_crafted_name_cannot_flip_another_files_verdict(cfg, monkeypatch, tmp_path) -> None:
    """The consequence, end to end: real emitter bytes through the real fold.

    `bad.py` does not compile. Whatever the file beside it is called, the
    digest for `bad.py` must not read `validate: ok`.
    """
    evil = tmp_path / FORGED
    evil.write_text("x = 1\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n", encoding="utf-8")

    emitted = supertool.op_validate_multi([str(evil), str(bad)])

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                           stdout=emitted, stderr="")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    digests = rs._validate_paths([str(evil), str(bad)])

    assert digests[str(bad)] != "validate: ok", digests
    assert "err" in (digests[str(bad)] or ""), digests


def test_an_ordinary_path_is_echoed_unchanged(cfg, tmp_path) -> None:
    """The guarantee must not cost the header its job: naming the file."""
    ok = tmp_path / "ok.py"
    ok.write_text("x = 1\n", encoding="utf-8")

    out = supertool.op_validate(str(ok))

    assert out.splitlines()[0] == f"validate: {ok}"


# ---------------------------------------------------------------------------
# #881, second half — a fold that cannot account for its inputs must say so
# ---------------------------------------------------------------------------

def test_a_block_count_mismatch_is_reported_not_zip_truncated(monkeypatch, tmp_path) -> None:
    """`zip` silently drops the tail. Silence here reads as a pass (#880)."""
    a = tmp_path / "a.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b = tmp_path / "b.py"
    b.write_text("y = 2\n", encoding="utf-8")

    def fake_run(cmd, **kw):
        # One block for two files — whatever the cause, the fold cannot know
        # which file it belongs to.
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="validate: a.py\npy-compile  : ok          (1ms)\n", stderr="")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    digests = rs._validate_paths([str(a), str(b)])

    assert all(d is not None for d in digests.values()), digests
    assert all("not checked" in str(d) for d in digests.values()), digests
    assert all(d != "validate: ok" for d in digests.values()), digests


# ---------------------------------------------------------------------------
# #882 — the payload route and the colon form must agree on every path
# ---------------------------------------------------------------------------

#: Refused and allowed cases together: a test that only feeds it escapes cannot
#: tell a working check from one that refuses everything.
PARITY_PATHS = [
    "/etc/hosts",
    "../" * 6 + "etc/hosts",
    "supertool.py",
    "tests/conftest.py",
]


def _refused(out: str) -> bool:
    return "escapes cwd" in out


@pytest.mark.parametrize("path", PARITY_PATHS)
def test_payload_and_colon_form_agree_on_every_path(cfg, path) -> None:
    """The post-condition. `_safe_path` is NOT stubbed — that stub is the bug."""
    colon = supertool.dispatch(f"validate:{path}")
    singular = supertool._read_op_from_payload("validate", {"path": path})
    plural = supertool._read_op_from_payload("validate", {"paths": [path]})

    assert _refused(singular) == _refused(colon), (
        f"path={path!r}\ncolon: {colon}\npayload(path): {singular}")
    assert _refused(plural) == _refused(colon), (
        f"path={path!r}\ncolon: {colon}\npayload(paths): {plural}")


def test_the_escape_is_refused_at_all_and_not_merely_agreed_upon(cfg) -> None:
    """Pins the direction: `/etc/hosts` is refused, not jointly allowed."""
    assert _refused(supertool.dispatch("validate:/etc/hosts"))
    assert _refused(supertool._read_op_from_payload("validate", {"path": "/etc/hosts"}))
    assert _refused(supertool._read_op_from_payload("validate", {"paths": ["/etc/hosts"]}))
    assert _refused(supertool._read_op_from_payload(
        "validate", {"paths": ["supertool.py", "/etc/hosts"]}))


def test_an_in_tree_path_still_validates_through_the_payload(cfg) -> None:
    """The guard must not cost the route its feature."""
    out = supertool._read_op_from_payload("validate", {"path": "supertool.py"})
    assert out.startswith("validate: supertool.py")
    assert not _refused(out)
