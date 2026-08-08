"""#962 / #963 / #986 — three ways git-commit's own output misleads the reader.

Two of the three issues were filed against behaviour that does not exist, and
those false premises are pinned here so they are not filed a fourth time (the
payload claim in #986 is the *third* airing of the claim already refuted by
tests/test_git_commit_refusals_1003.py and tests/test_git_commit_payload_route.py):

  - #963 claims the ':'-split refusal's `@-` example folds the paths into
    `message`. It does not, and never has — `_colon_split_refusal` has emitted
    a separate `paths = [...]` list since it landed in #751/#771 (4b991c5).
    It also claims the single-colon suggestion commits under a mangled
    subject. It does not: `:::` is the outer separator, so the reconstructed
    line commits the exact subject and stages the exact path.

  - #986 claims `git-commit:@payload` has no way to stage. It has: `paths`.

What is real, and what these tests pin:

  #962  `git-commit:::amend` commits with the subject `amend`. There is no
        amend route, so the op silently reinterprets an instruction as
        content and produces a commit that has to be undone.

  #963  (as actually hit, twice, rather than as written) A refusal prints the
        full list of candidate paths and then a remedy naming only the first
        three of them. Pasting the remedy commits a silent subset of what the
        reader was just shown — a remedy that produces the wrong commit,
        carrying the tool's authority. The `git-push`-shaped variant in the
        left-behind hint is worse still: it glues a literal '…' onto the last
        pathspec.

  #986  (the two residual claims, both real) The `nothing staged` refusal
        names only the colon route, to a caller who may be on the payload
        route precisely because their message will not survive it; and a
        pathspec that failed to add never states what the separator is.

The bar every remedy here has to clear: a reader who pastes it gets either
what they were shown, or a template that is visibly a template. Never a
subset that looks complete.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"
COAUTHOR = "Test Bot <bot@example.invalid>"

_COMMIT_PATH = REPO / "presets" / "git" / "commit.py"
_spec = importlib.util.spec_from_file_location("git_commit_962", _COMMIT_PATH)
assert _spec is not None and _spec.loader is not None
commit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit)


def _repo(tmp_path: Path, names=("a.txt", "b.txt", "sub/c.txt")) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n', encoding="utf-8")
    for name in names:
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    return work


def _touch(work: Path, names) -> None:
    for name in names:
        (work / name).write_text("2\n", encoding="utf-8")


def _run(args: list[str], cwd: Path, stdin: str = "", env_extra=None) -> str:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    env.pop("SUPERTOOL_ALLOW_LITERAL_AMEND", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.stdout + proc.stderr


def _subject(work: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=format:%s"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
        errors="replace",
    ).stdout


def _count(work: Path) -> int:
    return len(subprocess.run(
        ["git", "log", "--pretty=format:%h"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
        errors="replace",
    ).stdout.split())


# --------------------------------------------------------------------------
# #962 — 'amend' is an instruction, and was taken as content
# --------------------------------------------------------------------------

def test_bare_amend_is_refused_and_commits_nothing(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    _touch(work, ["a.txt"])
    before = _count(work)
    out = _run(["git-commit:::amend:::a.txt"], cwd=work)
    assert _count(work) == before, f"a commit was made: {out}"
    assert _subject(work) == "seed"
    assert "amend" in out.lower() and "nothing" in out.lower()


def test_amend_refusal_names_the_missing_route_and_the_git_fallback(
    tmp_path: Path,
) -> None:
    """A refusal that does not say what to do instead is half a refusal."""
    work = _repo(tmp_path)
    _touch(work, ["a.txt"])
    out = _run(["git-commit:::amend:::a.txt"], cwd=work)
    assert "no amend route" in out.lower()
    assert "git commit --amend" in out
    assert "push" in out.lower(), "must warn about rewriting published history"


def test_double_dash_amend_is_refused_the_same_way(tmp_path: Path) -> None:
    """`--amend` reaches `git commit -m` as a value, not a flag — same defect."""
    work = _repo(tmp_path)
    _touch(work, ["a.txt"])
    before = _count(work)
    out = _run(["git-commit:::--amend:::a.txt"], cwd=work)
    assert _count(work) == before, f"a commit was made: {out}"
    assert "no amend route" in out.lower()


def test_literal_amend_subject_stays_reachable_via_the_named_escape(
    tmp_path: Path,
) -> None:
    """'amend' is a legal English word; the refusal must not be a dead end."""
    work = _repo(tmp_path)
    _touch(work, ["a.txt"])
    out = _run(["git-commit:::amend:::a.txt"], cwd=work)
    assert "SUPERTOOL_ALLOW_LITERAL_AMEND" in out
    out2 = _run(["git-commit:::amend:::a.txt"], cwd=work,
                env_extra={"SUPERTOOL_ALLOW_LITERAL_AMEND": "1"})
    assert _subject(work) == "amend", out2


def test_a_message_merely_containing_amend_is_untouched(tmp_path: Path) -> None:
    """The refusal is exact-match, not a 'looks like a subcommand' heuristic."""
    work = _repo(tmp_path)
    _touch(work, ["a.txt"])
    out = _run(["git-commit:::fix(git)- amend the receipt wording:::a.txt"],
               cwd=work)
    assert _subject(work) == "fix(git)- amend the receipt wording", out


# --------------------------------------------------------------------------
# #963 (as hit) — a remedy that names a subset of what it just listed
# --------------------------------------------------------------------------

MANY = tuple(f"f{i:02d}.txt" for i in range(1, 16))


def test_nothing_staged_remedy_names_every_path_it_listed(tmp_path: Path) -> None:
    """15 listed, 3 in the remedy: pasting it commits a silent subset."""
    work = _repo(tmp_path, names=MANY)
    _touch(work, MANY)
    out = _run(["git-commit:::MESSAGE"], cwd=work)
    remedy = [l for l in out.splitlines() if "git-commit:::MESSAGE:::" in l]
    assert remedy, out
    line = remedy[0]
    missing = [p for p in MANY if p not in line]
    assert not missing, f"remedy omits {missing}: {line}"


def test_left_behind_remedy_names_every_path_and_no_ellipsis_pathspec(
    tmp_path: Path,
) -> None:
    """`…` glued to the last pathspec is not a pathspec anyone can paste."""
    work = _repo(tmp_path, names=MANY)
    _touch(work, MANY)
    out = _run([f"git-commit:::chore: one:::{MANY[0]}"], cwd=work)
    remedy = [l for l in out.splitlines() if "git-commit:::MESSAGE:::" in l]
    assert remedy, out
    line = remedy[0]
    assert "…'" not in line and not line.rstrip().endswith("…"), (
        f"ellipsis glued into a pathspec: {line}"
    )
    missing = [p for p in MANY[1:] if p not in line]
    assert not missing, f"remedy omits {missing}: {line}"


def test_a_truncated_list_gets_a_template_not_a_fabricated_subset(
    tmp_path: Path,
) -> None:
    """Past the display cap the honest answer is 'name them', not a guess.

    A refusal that dumps 200 pathspecs is its own defect; so is one that
    dumps the first 20 of 200 and lets them read as the whole set.
    """
    lots = tuple(f"g{i:03d}.txt" for i in range(1, 41))
    work = _repo(tmp_path, names=lots)
    _touch(work, lots)
    out = _run(["git-commit:::MESSAGE"], cwd=work)
    remedy = [l for l in out.splitlines() if "git-commit:::MESSAGE:::" in l]
    assert remedy, out
    line = remedy[0]
    named = [p for p in lots if p in line]
    assert not named, f"a partial path list reads as complete: {line}"
    assert "PATH" in line
    assert "40" in out and "not shown" in out.lower()


# --------------------------------------------------------------------------
# #986 — the residual two claims
# --------------------------------------------------------------------------

def test_nothing_staged_offers_the_payload_route_too(tmp_path: Path) -> None:
    """The caller may be on the payload route *because* of the tokenizer."""
    work = _repo(tmp_path)
    _touch(work, ["a.txt"])
    out = _run(["git-commit:@-"], cwd=work,
               stdin="message = 'subject with no paths key'\n")
    assert "git-commit:@-" in out, out
    assert "paths = [" in out, out
    assert '"a.txt"' in out, out


def test_pathspec_failure_states_the_separator(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    _touch(work, ["a.txt", "b.txt"])
    out = _run(["git-commit:::fix stuff:::a.txt,b.txt"], cwd=work)
    assert ":::" in out
    assert "separat" in out.lower()


def test_comma_joined_pathspec_gets_the_split_suggestion(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    _touch(work, ["a.txt", "b.txt"])
    out = _run(["git-commit:::fix stuff:::a.txt,b.txt"], cwd=work)
    assert "git-commit:::MESSAGE:::a.txt:::b.txt" in out, out


def test_a_real_comma_in_a_filename_is_not_re_split(tmp_path: Path) -> None:
    """A file genuinely named `a,b.txt` must not be guessed apart."""
    work = _repo(tmp_path, names=("a,b.txt",))
    _touch(work, ["a,b.txt"])
    out = _run(["git-commit:::chore- comma name:::a,b.txt"], cwd=work)
    assert _subject(work) == "chore- comma name", out
    assert "git-commit:::MESSAGE:::a:::b.txt" not in out


# --------------------------------------------------------------------------
# The two false premises, pinned so they are not re-filed
# --------------------------------------------------------------------------

def test_963_the_colon_refusal_keeps_paths_out_of_the_message_key(
    tmp_path: Path,
) -> None:
    work = _repo(tmp_path, names=("tests/test_x.py",))
    _touch(work, ["tests/test_x.py"])
    out = _run(
        ["git-commit:::test(gh-prs tier)::: pin the vocabulary (#939)"
         ":::tests/test_x.py"],
        cwd=work,
    )
    msg_line = [l for l in out.splitlines() if l.strip().startswith("message = ")]
    assert msg_line, out
    assert "tests/test_x.py" not in msg_line[0], msg_line[0]
    assert 'paths = ["tests/test_x.py"]' in out


def test_963_the_reconstructed_colon_form_commits_the_intended_subject(
    tmp_path: Path,
) -> None:
    """The remedy the refusal hands back must actually work when pasted."""
    work = _repo(tmp_path, names=("tests/test_x.py",))
    _touch(work, ["tests/test_x.py"])
    out = _run(
        ["git-commit:::test(gh-prs tier)::: pin the vocabulary (#939)"
         ":::tests/test_x.py"],
        cwd=work,
    )
    suggested = [
        l.strip() for l in out.splitlines()
        if l.strip().startswith("./supertool 'git-commit:::")
    ]
    assert suggested, out
    op = suggested[0].split("'", 1)[1].rsplit("'", 1)[0]
    _run([op], cwd=work)
    assert _subject(work) == "test(gh-prs tier): pin the vocabulary (#939)"


def test_986_the_payload_route_can_stage(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    _touch(work, ["a.txt", "b.txt"])
    out = _run(["git-commit:@-"], cwd=work,
               stdin="message = 'fix(x): only a'\npaths = [\"a.txt\"]\n")
    assert _subject(work) == "fix(x): only a", out
    staged = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
        errors="replace",
    ).stdout.split()
    assert staged == ["a.txt"], staged
