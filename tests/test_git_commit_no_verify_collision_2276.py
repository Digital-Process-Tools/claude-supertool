"""#2276 -- `--no-verify`'s sentinel extraction has no filename-collision
guard, unlike its twin `--all`.

`_ALL_TOKEN` refuses when git already knows a real, tracked file spelled
`--all` (#1137): the token means two things at once and the op declines to
guess -- but ONLY when `--all` is the sole path argument; paired with a
named path it is refused earlier, for a different reason
(`_all_with_paths_refusal`). `_NO_VERIFY_TOKEN` (#2205) copied the
extraction shape -- pulled out of `paths` before anything downstream reads
the list -- but not the guard, nor its scope.

A tracked file literally named `--no-verify` would be silently dropped from
`paths`, and if that leaves `paths` empty the op falls through to the
whole-index warning: the #1228 incident shape, one sentinel over. The guard
added here is deliberately scoped to exactly that case -- `--no-verify` as
the SOLE path argument -- because unlike `--all`, combining `--no-verify`
with a real named path is the documented, routine shape
(`git-commit:::MSG:::A:::--no-verify`). An unscoped guard was tried first
and dropped in self-review: it refused that ordinary combined call forever,
in any repo merely containing an unrelated file spelled `--no-verify`
anywhere on disk, named or not -- see
`test_no_verify_alongside_a_named_path_is_not_blocked_by_an_unrelated_file`.

Tests: the collision must refuse when the sentinel is the only path given
(`test_no_verify_is_refused_...`), the payload route reaches the same
guard (`test_no_verify_collision_via_payload_route_also_refuses`), an
ordinary `--no-verify` call with no such file present must still work
exactly as before (`test_no_verify_still_works_when_no_such_file_exists`),
and a `--no-verify` paired with a named path must not be blocked by an
unrelated, unnamed file elsewhere merely sharing the sentinel's spelling
(`test_no_verify_alongside_a_named_path_is_not_blocked_by_an_unrelated_file`).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"
COAUTHOR = "Test Bot <bot@example.invalid>"


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n', encoding="utf-8")
    (work / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    return work


def _run(args: list, cwd: Path, stdin: str = "") -> tuple:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _head_sha(work: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
    ).stdout.strip()


# --- must fire: a real file named `--no-verify` makes the token ambiguous --


def test_no_verify_is_refused_when_a_file_of_that_name_exists(tmp_path: Path) -> None:
    """`--no-verify` is a legal filename. When git knows one, the sentinel
    means two things and the op must not silently pick 'hook-skip' and drop
    the file -- it declines, the way `--all` already does."""
    work = _repo(tmp_path)
    (work / "--no-verify").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "--no-verify"], cwd=work, check=True)
    before = _head_sha(work)

    code, out = _run(["git-commit:::a message:::--no-verify"], cwd=work)

    assert "ERROR" in out, out
    assert _head_sha(work) == before, out  # nothing landed, nothing dropped


def test_no_verify_collision_via_payload_route_also_refuses(tmp_path: Path) -> None:
    """The payload route reaches the same argv (`no_verify = true` is folded
    into the same sentinel before commit.py ever sees it), so the guard must
    not be colon-route-only."""
    work = _repo(tmp_path)
    (work / "--no-verify").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "--no-verify"], cwd=work, check=True)
    before = _head_sha(work)

    payload = 'message = "a message"\nno_verify = true\n'
    code, out = _run(["git-commit:@-"], cwd=work, stdin=payload)

    assert "ERROR" in out, out
    assert _head_sha(work) == before, out


# --- must NOT fire: no collision, ordinary hook-skip still works ----------


def test_no_verify_still_works_when_no_such_file_exists(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    before = _head_sha(work)

    code, out = _run(["git-commit:::a message:::a.txt:::--no-verify"], cwd=work)

    assert code == 0, out
    assert _head_sha(work) != before, out
    assert "HOOKS SKIPPED" in out, out


def test_no_verify_alongside_a_named_path_is_not_blocked_by_an_unrelated_file(
    tmp_path: Path,
) -> None:
    """`--no-verify` paired with a real named path is the documented, routine
    shape. An unrelated, untracked file that merely happens to be spelled
    `--no-verify` -- never staged, never named in this call -- must not turn
    every future combined call into a permanent refusal: after stripping the
    sentinel, `paths` still holds `a.txt`, so nothing here would fall through
    to the #1228 whole-index shape the guard exists to prevent."""
    work = _repo(tmp_path)
    (work / "--no-verify").write_text("unrelated\n", encoding="utf-8")
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    before = _head_sha(work)

    code, out = _run(["git-commit:::a message:::a.txt:::--no-verify"], cwd=work)

    assert code == 0, out
    assert _head_sha(work) != before, out
    assert "HOOKS SKIPPED" in out, out
