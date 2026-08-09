"""#1155 + #1137 — git-commit's no-PATHS refusal, and the explicit `--all`.

#1155 as filed says a *multi-line* message "stages nothing". That is not what
happens. A multi-line message with `:::PATHS` on the colon route commits fine,
and is pinned below so the claim is not re-filed. What is real is that a call
with no PATHS at all — at any message length — is refused with `nothing
staged`, which names the symptom. The cause is that PATHS were never given and
this op never stages on its own, and that sentence has to arrive first because
the core `--- op ---` header has already replayed the whole message above it.

#1137 — the refusal counts the dirty paths and then offers no way to say "those
ones", so satisfying it meant dropping to raw `git status --porcelain` to
rebuild the list the op had just printed. `--all` is that opt-in. The bare form
keeps refusing, and the receipt names every path that went in rather than
capping at 20, because the whole point is a record of what was committed.
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
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n',
                                          encoding="utf-8")
    (work / "a.txt").write_text("1\n", encoding="utf-8")
    (work / "b.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    return work


def _run(args: list[str], cwd: Path, stdin: str = "") -> str:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.stdout + proc.stderr


def _head_subject(work: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=format:%s"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
        errors="replace",
    ).stdout


def _committed_paths(work: Path) -> set:
    out = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
        errors="replace",
    ).stdout
    return {l for l in out.splitlines() if l.strip()}


def _first_error_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("ERROR:"):
            return line
    raise AssertionError("no ERROR: line in output:\n" + out)


# --- #1155: the refusal must lead with the cause --------------------------


def test_no_paths_refusal_leads_with_the_missing_paths(tmp_path: Path) -> None:
    """`nothing staged` is the symptom. The cause is that no PATHS were given
    and this op does not stage on its own — and it has to be the first line,
    because everything above it is the caller's own message replayed back."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")

    first = _first_error_line(_run(["git-commit:::a message"], cwd=work))

    assert "PATHS" in first, first
    assert _head_subject(work) == "seed"


def test_no_paths_refusal_says_the_op_does_not_stage_by_itself(tmp_path: Path) -> None:
    """Naming the missing argument is only half of it; a reader who thinks the
    op stages for you needs telling that it never does."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    out = _run(["git-commit:::a message"], cwd=work)
    assert "never stages" in out, out


def test_named_paths_that_stage_nothing_do_not_claim_no_paths(tmp_path: Path) -> None:
    """The other arm. PATHS *were* given and still nothing staged — saying
    'no PATHS were given' there would be a false statement about the call."""
    work = _repo(tmp_path)
    (work / "b.txt").write_text("2\n", encoding="utf-8")
    out = _run(["git-commit:::a message:::a.txt"], cwd=work)
    first = _first_error_line(out)
    assert "no PATHS" not in first, first
    assert "1 path" in first, first
    assert _head_subject(work) == "seed"


def test_multiline_message_with_paths_commits_on_the_colon_route(tmp_path: Path) -> None:
    """#1155's headline claim, refuted in place so it is not re-filed: the
    colon route carries a multi-line body and stages exactly as it says."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    out = _run(["git-commit:::subject line\n\nbody one\nbody two:::a.txt"],
               cwd=work)
    assert "ERROR" not in out, out
    assert _head_subject(work) == "subject line"
    assert _committed_paths(work) == {"a.txt"}


# --- #1137: the explicit opt-in -------------------------------------------


def test_bare_form_still_refuses(tmp_path: Path) -> None:
    """The refusal is the feature. `--all` is an opt-in, not a new default."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    out = _run(["git-commit:::a message"], cwd=work)
    assert "ERROR" in out
    assert _head_subject(work) == "seed"


def test_all_stages_modified_and_untracked_and_names_them(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    (work / "new.txt").write_text("n\n", encoding="utf-8")

    out = _run(["git-commit:::a message:::--all"], cwd=work)

    assert "ERROR" not in out, out
    assert _head_subject(work) == "a message"
    assert _committed_paths(work) == {"a.txt", "new.txt"}
    assert "a.txt" in out and "new.txt" in out, out


def test_all_receipt_names_every_path_not_the_first_twenty(tmp_path: Path) -> None:
    """#1137 asked for a receipt that records which 24 paths went in. A list
    capped at 20 under a green tick is the #963 defect: a subset presented as
    the whole."""
    work = _repo(tmp_path)
    for i in range(24):
        (work / f"f{i:02d}.txt").write_text("x\n", encoding="utf-8")

    out = _run(["git-commit:::a message:::--all"], cwd=work)

    assert "ERROR" not in out, out
    assert len(_committed_paths(work)) == 24
    for i in range(24):
        assert f"f{i:02d}.txt" in out, out
    assert "4 more" not in out, out


def test_all_on_a_clean_tree_refuses_rather_than_committing_nothing(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    out = _run(["git-commit:::a message:::--all"], cwd=work)
    assert "ERROR" in out, out
    assert "clean" in out.lower(), out
    assert _head_subject(work) == "seed"


def test_all_is_refused_when_a_file_of_that_name_exists(tmp_path: Path) -> None:
    """`--all` is a legal filename. When git knows one, the token means two
    things and the op must not pick — it declines and names the route that
    can say which was meant."""
    work = _repo(tmp_path)
    (work / "--all").write_text("x\n", encoding="utf-8")
    (work / "a.txt").write_text("2\n", encoding="utf-8")

    out = _run(["git-commit:::a message:::--all"], cwd=work)

    assert "ERROR" in out, out
    assert "paths" in out, out
    assert _head_subject(work) == "seed"


def test_all_must_be_the_only_path_argument(tmp_path: Path) -> None:
    """`--all` next to a named path is a contradiction — one of the two is
    ignored whichever way it resolves, and a silent ignore under a green tick
    is how the wrong commit gets made."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    (work / "b.txt").write_text("2\n", encoding="utf-8")

    out = _run(["git-commit:::a message:::--all:::a.txt"], cwd=work)

    assert "ERROR" in out, out
    assert _head_subject(work) == "seed"


def test_all_works_through_the_payload_route(tmp_path: Path) -> None:
    """The payload route reaches the same argv, so the opt-in must spell the
    same there rather than being colon-route-only."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    (work / "new.txt").write_text("n\n", encoding="utf-8")

    out = _run(["git-commit:@-"], cwd=work,
               stdin='message = """subject: with a colon"""\npaths = ["--all"]\n')

    assert "ERROR" not in out, out
    assert _head_subject(work) == "subject: with a colon"
    assert _committed_paths(work) == {"a.txt", "new.txt"}


def test_nothing_staged_refusal_offers_the_all_opt_in(tmp_path: Path) -> None:
    """The refusal counted the paths; it now also says how to accept them."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    out = _run(["git-commit:::a message"], cwd=work)
    assert ":::--all" in out, out
