"""`_PARALLEL_SAFE_OPS` said "read-only" and carried `format_staged` (#1244).

`format_staged` shells formatters over every staged file and rewrites them.
Two production consumers read the set, and it is the wrong answer for both:

  * `_main` runs a whole batch on a ThreadPool when every op is "safe", so a
    formatter's write races a sibling `read` of the same file. Measured on
    master with a 0.6s formatter: the same two-op call returned the
    post-format bytes sequentially and the *pre*-format bytes in parallel,
    while the file on disk held the post-format bytes either way. A rendered
    file body that no longer exists, with no marker saying so.
  * `dispatch` skips `_path_meta_bulk_drop()` for a "safe" op, so the
    repo-wide `git status` snapshot survives a write that changed the tree it
    describes.

`_OP_SAFETY_BUILTIN` has declared `format_staged: "writes"` since #1231, and
its comment says in as many words that this set is not a read-only oracle.
Two declarations of the same fact disagreed; the structural test below is what
stops the next one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import supertool
from _changelog_findable import assert_change_is_findable


def test_format_staged_is_not_parallel_safe() -> None:
    assert "format_staged" not in supertool._PARALLEL_SAFE_OPS
    assert not supertool._is_parallel_safe("format_staged")
    assert not supertool._is_parallel_safe("format_staged::black")


def test_parallel_safe_set_never_contradicts_declared_safety() -> None:
    """The set may only hold ops `_OP_SAFETY_BUILTIN` calls read-only.

    Keyed off the declaration site rather than a hand-kept list, because the
    defect was two sources of the same truth drifting apart. An op absent from
    `_OP_SAFETY_BUILTIN` is not checked here -- `blame` is one -- since that
    table only covers built-ins.
    """
    declared = supertool._OP_SAFETY_BUILTIN
    contradictions = {
        op: declared[op]
        for op in supertool._PARALLEL_SAFE_OPS
        if op in declared and declared[op] != "read-only"
    }
    assert contradictions == {}, (
        "in _PARALLEL_SAFE_OPS but not declared read-only: " + repr(contradictions)
    )


def test_format_staged_drops_the_path_meta_snapshot(tmp_path, monkeypatch) -> None:
    """A write invalidates the repo-wide status snapshot, formatters included."""
    monkeypatch.chdir(tmp_path)
    supertool._PATH_META_BULK.clear()
    supertool._PATH_META_BULK[str(tmp_path)] = {"codes": {}, "taken_ns": 1}

    supertool.dispatch("format_staged")

    assert supertool._PATH_META_BULK == {}, (
        "format_staged rewrites staged files -- the snapshot cannot outlive it"
    )
    supertool._PATH_META_BULK.clear()


def _fmt_repo(tmp_path: Path) -> Path:
    """A git repo with one staged file and a slow formatter that rewrites it.

    Same cross-platform shape as `tests/test_formatters_deferred.py`: the
    interpreter and the helper script go into the `cmd` string with forward
    slashes, because supertool splits it with `shlex` in POSIX mode and a
    Windows tmp_path's backslashes would be eaten. The target path is baked
    into the helper with `!r` rather than passed through more quoting.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*argv: str) -> None:
        subprocess.run(argv, cwd=str(repo), check=True, capture_output=True)

    run("git", "init", "-q", ".")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    target = repo / "f.txt"
    target.write_text("ORIGINAL" + chr(10), encoding="utf-8")
    helper = repo / "_slowfmt.py"
    helper.write_text(
        "import time" + chr(10)
        + "time.sleep(1.5)" + chr(10)
        + "open({0!r}, 'w').write('FORMATTED' + chr(10))".format(str(target))
        + chr(10),
        encoding="utf-8",
    )
    exe = sys.executable.replace(chr(92), "/")
    helper_fwd = str(helper).replace(chr(92), "/")
    (repo / ".supertool.json").write_text(
        json.dumps({"formatters": {
            "slowfmt": {"cmd": exe + " " + helper_fwd, "match": "*.txt"},
        }}) + chr(10),
        encoding="utf-8",
    )
    run("git", "add", "f.txt")
    return repo


def test_a_parallel_batch_never_reads_a_file_a_sibling_op_is_rewriting(
    tmp_path,
) -> None:
    """The end-to-end shape: `format_staged` + `read` of the same file.

    The GREEN assertion carries no timing at all -- once `format_staged` is
    outside the safe set the batch is sequential, so the read is ordered after
    the write by construction. Only the RED depended on the formatter being
    slower than the read, and 1.5s against a one-line file is not a race.
    """
    repo = _fmt_repo(tmp_path)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["SUPERTOOL_PARALLEL"] = "4"
    r = subprocess.run(
        [sys.executable, str(Path(supertool.__file__).parent / "supertool.py"),
         "format_staged", "read:f.txt"],
        cwd=str(repo), capture_output=True, text=True, env=env, timeout=120,
        encoding="utf-8", errors="replace",
    )
    on_disk = (repo / "f.txt").read_text(encoding="utf-8").strip()
    assert on_disk == "FORMATTED", "the formatter did not run: " + r.stdout + r.stderr
    assert "ORIGINAL" not in r.stdout, (
        "read rendered bytes the sibling format_staged had already replaced:"
        + chr(10) + r.stdout
    )
    assert "FORMATTED" in r.stdout


def test_a_changelog_fragment_exists() -> None:
    assert_change_is_findable(1244)
