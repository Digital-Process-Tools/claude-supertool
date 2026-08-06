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

**#886, the output again, one character over.** Both fixes above were written
against `\\n`. `presets/git/resolve.py` folds blocks with `str.splitlines()`,
which splits on ten separators, and the flattener neutralised eight of them —
U+2028 and U+2029 passed through, so the crafted filename still wrote its own
headers. The question that catches this is not "who parses this prefix" but
"what does the consumer's `splitlines()` split on, and does the flattener
cover exactly that set?"

**#885, the input again, one op over.** `validate` was the only payload read
op the #882 fix reached. `grep`, `around`, `grep_around` and `between` took
their path straight from the payload into the op, and those ops have no
containment of their own — so a payload returned the contents of any file on
disk. Same shape, four more doors.

What is asserted here is the post-condition, never the mechanism: the header
count comes off **real `op_validate_multi` output**, not from observing that a
flattener was called — which would pass on a version that flattens the wrong
field. And containment parity is asserted with `_safe_path` **live**. The suite
that shipped #882 stubbed it to identity on exactly this path; a test that
switches the detector off cannot see this class of defect at all.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
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

# ---------------------------------------------------------------------------
# #886 — a newline was never the only separator the consumer splits on
# ---------------------------------------------------------------------------

#: Every separator `str.splitlines()` splits on, which is the set that decides
#: how many lines the fold in `presets/git/resolve.py` sees. CRLF is left out
#: only because it is CR followed by LF, and both are here already.
SPLITLINES_SEPARATORS = [
    "\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029",
]


def _forged_name(sep: str) -> str:
    """#881's crafted filename with `sep` where its newlines were."""
    return f"evil{sep}validate: forged.py{sep}ok          : ok{sep}.py"


@pytest.mark.skipif(os.name == "nt",
                    reason="NTFS cannot hold these separators in a name")
@pytest.mark.parametrize("sep", SPLITLINES_SEPARATORS)
def test_no_separator_a_filename_can_hold_writes_a_second_block(
        cfg, tmp_path, sep) -> None:
    """The #881 post-condition over the whole set, off real emitter output."""
    evil = tmp_path / _forged_name(sep)
    evil.write_text("x = 1" + chr(10), encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("def (:" + chr(10), encoding="utf-8")

    out = supertool.op_validate_multi([str(evil), str(bad)])

    headers = [ln for ln in out.splitlines() if ln.startswith("validate:")]
    assert len(headers) == 2, (
        f"{len(headers)} headers for 2 files, separator {sep!r}:" + chr(10)
        + repr(out))


@pytest.mark.skipif(os.name == "nt",
                    reason="NTFS cannot hold these separators in a name")
@pytest.mark.parametrize("sep", SPLITLINES_SEPARATORS)
def test_no_separator_flips_another_files_verdict(
        cfg, monkeypatch, tmp_path, sep) -> None:
    """The consequence: `bad.py` does not compile, whatever sits beside it."""
    evil = tmp_path / _forged_name(sep)
    evil.write_text("x = 1" + chr(10), encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("def (:" + chr(10), encoding="utf-8")

    emitted = supertool.op_validate_multi([str(evil), str(bad)])

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                           stdout=emitted, stderr="")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    digests = rs._validate_paths([str(evil), str(bad)])

    assert digests[str(bad)] != "validate: ok", (sep, digests)


def test_the_flattener_covers_exactly_what_the_consumer_splits_on() -> None:
    """The rule stated once: one line out, for every separator, both ways in."""
    for sep in SPLITLINES_SEPARATORS:
        flattened = supertool._flat_field(f"a{sep}b")
        assert len(flattened.splitlines()) == 1, (sep, flattened)


def test_the_no_presets_fallback_holds_the_same_line(monkeypatch) -> None:
    """#884 consolidated onto `presets/`; the fallback must not be weaker."""
    monkeypatch.setattr(supertool, "_UNTRUSTED_FLAT_TRIED", True)
    monkeypatch.setattr(supertool, "_UNTRUSTED_FLAT", None)
    for sep in SPLITLINES_SEPARATORS:
        flattened = supertool._flat_field(f"a{sep}b")
        assert len(flattened.splitlines()) == 1, (sep, flattened)


#: Paths an ordinary caller actually hands `validate:` — accented, CJK, spaces,
#: a Windows drive letter, a colon in a name. None may be touched by the
#: widening: #884 bought "an ordinary path is echoed byte-identical" and this
#: is the receipt for it.
ORDINARY_PATHS = [
    "supertool.py",
    "tests/conftest.py",
    "./a b/c-d_e.PHP",
    "presets/ete.xml",
    "src/Entite/Reglement.php",
    "C:" + chr(92) + "Users" + chr(92) + "dev" + chr(92) + "x.py",
    "a:b:c.py",
    "docs/漢字/x.py",
    "src/été/naïve.py",
]


@pytest.mark.parametrize("path", ORDINARY_PATHS)
def test_an_ordinary_path_passes_through_byte_identical(path) -> None:
    assert supertool._flat_field(path) == path


# ---------------------------------------------------------------------------
# #885 — every payload read op, not only `validate`
# ---------------------------------------------------------------------------

#: The two forms of each path-bearing read op, built from one `path` so both
#: routes are asked the same question. `between` is given its pattern mode:
#: symbol mode has no second path position to disagree about.
def _colon_form(op: str, path: str) -> str:
    return {
        "grep": f"grep:root:{path}",
        "around": f"around:root:{path}",
        "grep_around": f"grep_around:root:{path}",
        "between": f"between:re:root:root:{path}",
        "read": f"read:{path}",
        "validate": f"validate:{path}",
    }[op]


def _payload_form(op: str, path: str) -> dict:
    return {
        "grep": {"pattern": "root", "path": path},
        "around": {"pattern": "root", "path": path},
        "grep_around": {"pattern": "root", "path": path},
        "between": {"start": "root", "end": "root", "path": path},
        "read": {"path": path},
        "validate": {"path": path},
    }[op]


PAYLOAD_READ_OPS = ["grep", "around", "grep_around", "between", "read",
                    "validate"]


@pytest.mark.parametrize("op", PAYLOAD_READ_OPS)
@pytest.mark.parametrize("path", PARITY_PATHS)
def test_every_payload_read_op_agrees_with_its_colon_form(cfg, op, path) -> None:
    """The post-condition. `_safe_path` is NOT stubbed — that stub is the bug."""
    colon = supertool.dispatch(_colon_form(op, path))
    payload = supertool._read_op_from_payload(op, _payload_form(op, path))

    assert _refused(payload) == _refused(colon), (
        f"op={op} path={path!r}" + chr(10) + f"colon: {colon[:400]}" + chr(10)
        + f"payload: {payload[:400]}")


@pytest.mark.parametrize("op", PAYLOAD_READ_OPS)
def test_every_payload_read_op_refuses_an_escape(cfg, op) -> None:
    """Pins the direction: jointly allowing `/etc/hosts` is not agreement."""
    out = supertool._read_op_from_payload(op, _payload_form(op, "/etc/hosts"))
    assert _refused(out), f"op={op}: {out[:400]}"


@pytest.mark.parametrize("op", PAYLOAD_READ_OPS)
def test_every_payload_read_op_still_works_in_tree(cfg, op) -> None:
    """The guard must not cost the route its feature."""
    out = supertool._read_op_from_payload(op, _payload_form(op, "supertool.py"))
    assert not _refused(out), f"op={op}: {out[:400]}"


def test_both_payload_doors_refuse_from_a_real_cwd(tmp_path) -> None:
    """The two entry points, as a user meets them.

    `grep:@-` is the standalone door and `batch:@-` the second one; a fix on
    one function covers both only if both really pass through it. Run as a
    subprocess from a cwd whose `.supertool.json` says `allow_outside_cwd:
    false`, with the env opt-out removed — those two are what switch this rule
    off inside this repo, and a test that leaves them on asserts nothing.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"allow_outside_cwd": False}), encoding="utf-8")
    outside = str(ROOT / "supertool.py")
    env = dict(os.environ)
    env.pop("SUPERTOOL_ALLOW_OUTSIDE_CWD", None)

    doors = {
        "grep:@-": json.dumps({"pattern": "def", "path": outside}),
        "batch:@-": json.dumps(
            {"ops": [{"op": "grep", "pattern": "def", "path": outside}]}),
    }
    for arg, stdin in doors.items():
        run = subprocess.run(
            [sys.executable, str(ROOT / "supertool.py"), arg],
            input=stdin, capture_output=True, text=True, cwd=str(tmp_path),
            env=env, encoding="utf-8", errors="replace")
        assert "escapes cwd" in run.stdout, (
            f"{arg}:" + chr(10) + run.stdout[:800] + chr(10) + run.stderr[:400])

# ---------------------------------------------------------------------------
# #889 — the sentinel skip is a property of positional dispatch, not of paths
# ---------------------------------------------------------------------------

#: The four values `_containment_error` used to skip. `""` and `"."` are not
#: filenames — one means "no path given", the other *is* cwd — but `raw` and
#: `full` are names any repo may hold, and #884 moved the skip into the shared
#: helper, so every route inherited it.
SENTINEL_NAMES = ["raw", "full"]


@pytest.fixture
def sentinel_tree(cfg, monkeypatch, tmp_path):
    """A cwd holding `raw` and `full` as symlinks to a file outside it.

    The bug needs a file *named* like a sentinel. Feeding the helper the
    literal strings cannot see it: `raw` with nothing on disk resolves under
    cwd and is allowed either way, which is how the first audit of this delta
    called the area clean.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text("x = 1" + chr(10), encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "good.py").write_text("y = 2" + chr(10), encoding="utf-8")
    for name in SENTINEL_NAMES:
        os.symlink(secret, work / name)
    monkeypatch.chdir(work)
    return work


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on NTFS")
@pytest.mark.parametrize("name", SENTINEL_NAMES)
def test_a_file_named_like_a_sentinel_is_still_contained(sentinel_tree, name) -> None:
    """Every route that reaches `validate`, on a path it must refuse.

    `op_validate` and `op_validate_multi` have no `_safe_path` of their own, so
    the gate the routes share is the only gate `validate` has.
    """
    routes = {
        "payload path": supertool._read_op_from_payload("validate", {"path": name}),
        "payload paths": supertool._read_op_from_payload(
            "validate", {"paths": [name, "good.py"]}),
        "colon": supertool.dispatch(f"validate:{name}"),
        "colon list": supertool.dispatch(f"validate:{name},good.py"),
    }
    for route, out in routes.items():
        assert _refused(out), f"{route} allowed {name!r}: {out[:300]}"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on NTFS")
@pytest.mark.parametrize("name", SENTINEL_NAMES)
def test_the_other_read_ops_refuse_a_sentinel_name_too(sentinel_tree, name) -> None:
    """`read` was caught by its own chokepoint; the rest have none."""
    for op in PAYLOAD_READ_OPS:
        out = supertool._read_op_from_payload(op, _payload_form(op, name))
        assert _refused(out), f"{op} allowed {name!r}: {out[:300]}"


def test_an_in_tree_file_named_like_a_sentinel_still_works(sentinel_tree) -> None:
    """The guard must refuse the escape, not the name."""
    (sentinel_tree / "inner").mkdir()
    plain = sentinel_tree / "inner" / "raw"
    plain.write_text("z = 3" + chr(10), encoding="utf-8")

    out = supertool._read_op_from_payload("validate", {"path": "inner/raw"})

    assert not _refused(out), out[:300]


# ---------------------------------------------------------------------------
# #888 — a filename must not be able to answer "did the validator run?"
# ---------------------------------------------------------------------------

#: The two status strings `op_validate_multi` returns *instead of* any block.
#: Both were substring-tested against the combined stdout, which also carries
#: one `validate: <path>` header per file — so the filename could answer for
#: the batch. Flattening the header stops it adding a line; it never stopped it
#: containing a string.
STATUS_SENTINEL_NAMES = ["no validators.py", "no validators matched filter.py"]


@pytest.fixture
def resolve_tree(monkeypatch, tmp_path):
    """A real cwd `_validate_paths` can shell into: config, and files on disk."""
    (tmp_path / ".supertool.json").write_text(json.dumps(CFG), encoding="utf-8")
    (tmp_path / "bad.py").write_text("def (:" + chr(10), encoding="utf-8")
    (tmp_path / "good.py").write_text("x = 1" + chr(10), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_control_case_reports_the_real_verdict(resolve_tree) -> None:
    """Without which the assertions below cannot tell a fix from a no-op."""
    digests = rs._validate_paths(["good.py", "bad.py"])

    assert digests["good.py"] == "validate: ok", digests
    assert "err" in (digests["bad.py"] or ""), digests


@pytest.mark.parametrize("name", STATUS_SENTINEL_NAMES)
def test_a_filename_cannot_suppress_the_batchs_verdict(resolve_tree, name) -> None:
    """Off real `_validate_paths` output, with real files on disk.

    A neighbour's *name* must not decide whether this batch was checked. The
    post-condition is `bad.py`'s verdict, never "the sentinel branch was not
    taken" — the latter passes on a fix that merely lengthened the substring.
    """
    (resolve_tree / name).write_text("y = 2" + chr(10), encoding="utf-8")

    digests = rs._validate_paths([name, "bad.py"])

    assert "err" in (digests["bad.py"] or ""), (name, digests)
    assert digests["bad.py"] != "validate: ok", (name, digests)


def test_a_real_empty_validator_set_is_still_silent(monkeypatch, tmp_path) -> None:
    """The emitter's own status still reaches the caller when it is the truth."""
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"validators": {}}), encoding="utf-8")
    (tmp_path / "bad.py").write_text("def (:" + chr(10), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    digests = rs._validate_paths(["bad.py"])

    assert digests == {"bad.py": None}, digests

def test_a_list_valued_path_field_is_contained_too(cfg) -> None:
    """`path` accepts a list wherever `paths` does — the gate must read both.

    Caught in self-review of the #885 fix, which first read `path` as
    `str(p.get("path"))`: a list stringified to `"['/etc/hosts', …]"`, a
    nonsense name that resolves under cwd and is allowed, while
    `_validate_from_payload` went on to use the real list. The gate that
    replaced a working check has to accept every shape the check it replaced
    accepted — which is #882's lesson arriving one layer up.
    """
    assert _refused(supertool._read_op_from_payload(
        "validate", {"path": ["/etc/hosts", "supertool.py"]}))
    assert _refused(supertool._read_op_from_payload(
        "validate", {"paths": ["supertool.py", "/etc/hosts"]}))
