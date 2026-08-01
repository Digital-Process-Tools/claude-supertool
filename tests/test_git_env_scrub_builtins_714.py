"""#714 — the `GIT_*` scrub must cover BUILT-IN ops, not only preset launches.

#692 put the scrub in `_resolve_custom_op`, which is the launcher for *preset*
ops. Built-ins never pass through it, and core spawns git by itself in six
places feeding at least eight built-in ops:

    _run_git_ignore_query   `git -C ROOT check-ignore` / `ls-files --others
                            --ignored`  → the ignore pruning behind glob, grep,
                            tree, map
    _path_meta_suffix       `git status --porcelain --ignored=matching`
                            → the ` m`/` ?`/` !` marker on every read and on
                            workspace's meta line
    _branch_probe           `git symbolic-ref` / `rev-parse --short HEAD`
    op_workspace §Git       rev-parse, abbrev-ref, rev-list, status, log, blame
    op_validate_staged      `git diff --cached --name-only`
    op_format_staged        `git diff --cached --name-only`

None of them passes `env=`, so each inherits whatever `GIT_*` the parent had.
`.githooks/pre-commit` invokes `./supertool` and git exports `GIT_DIR` to every
hook it runs, so this is the caller this repo creates itself — same scenario
#692 was filed for, on the other half of the op table.

WHY IT IS WORSE THAN #692's CASE
Git exits 0. It is answering correctly, about a different repository. So the
`git?` decline from #705 has no failure to catch, and the marker is not absent,
it is *wrong* — a file described using another repo's index.

WHAT ACTUALLY FLIPS, AND WHAT DOES NOT
The issue relays an agent's observation that a gitignored file's ` !` became
``. That does NOT reproduce, and the reason matters for this fixture: a bare
`GIT_DIR` leak leaves the *work tree* at cwd, `.gitignore` is read from the
work tree, so the ignore verdict is identical either way. Nothing
ignore-derived can discriminate.

What flips is anything read out of the INDEX. A file tracked-and-modified in
the cwd repo reads ` m`; under the leak it is absent from the foreign index and
reads ` ?` — "untracked". That requires the two repos to be ASYMMETRIC: if both
carry the same path, the foreign index answers ` m` too and the leak is
invisible. Symmetric synthetic repos are why the reproduction failed on the
issue.

The control test below is load-bearing for exactly that reason: it asserts raw
git really does flip in this fixture, so a green suite means the scrub acted
rather than that the mechanism was never present.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import supertool

SUITE_ROOT = Path(__file__).resolve().parent.parent
SUPERTOOL = SUITE_ROOT / "supertool.py"

_ID = ["-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture"]

GIT_VARS = tuple(supertool.GIT_ENV_VARS)


def _git(args, cwd, env=None):
    return subprocess.run(
        ["git", *_ID, *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=60,
    )


def _make_repo(path: Path, name: str, branch: str, tracked: bool) -> Path:
    """A one-commit repo. `tracked` decides whether it carries `tracked.txt`.

    The asymmetry is the whole fixture: repoB has the file under test in its
    index, repoA does not. Only then does the foreign index give a different
    answer about the same file on disk.
    """
    path.mkdir(parents=True)
    _git(["init", "-q", "-b", branch], path)
    _git(["config", "user.email", "fixture@example.invalid"], path)
    _git(["config", "user.name", "fixture"], path)
    # Anchors `_auto_cwd_root` at the repo so the op under test runs from where
    # the test put it, not from some ancestor of the tmp dir.
    (path / ".supertool.json").write_text("{}\n")
    (path / f"{name}.txt").write_text(f"{name}\n")
    if tracked:
        (path / "tracked.txt").write_text("committed\n")
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", f"base {name}"], path)
    return path


def _fixture(tmp_path: Path):
    """repoA (no tracked.txt) + repoB (tracked.txt, locally modified)."""
    repo_a = _make_repo(tmp_path / "repoA", "a", "branch-of-repo-a", tracked=False)
    repo_b = _make_repo(tmp_path / "repoB", "b", "branch-of-repo-b", tracked=True)
    (repo_b / "tracked.txt").write_text("modified in B\n")
    return repo_a, repo_b


def _clean_env() -> dict:
    env = dict(os.environ)
    for name in GIT_VARS:
        env.pop(name, None)
    return env


def _run_op(op: str, cwd: Path, git_dir: Path | None = None):
    """Spawn supertool as its own process, GIT_DIR delivered via `env=` only.

    Never exported into this process: #416 flipped `core.bare` on the real
    clone exactly that way, and conftest's autouse fixture would strip it again
    anyway.
    """
    env = _clean_env()
    if git_dir is not None:
        env["GIT_DIR"] = str(git_dir)
    return subprocess.run(
        [sys.executable, str(SUPERTOOL), op],
        cwd=str(cwd), capture_output=True, text=True, timeout=180, env=env,
    )


# --------------------------------------------------------------------------
# The control — without this everything below could pass vacuously
# --------------------------------------------------------------------------

def test_control_raw_git_really_does_honour_the_leak(tmp_path):
    """Raw git, same fixture: ' M' becomes '??' under the foreign index.

    If this ever stops flipping — two repos that are secretly one, a git that
    stopped reading GIT_DIR, a fixture that accidentally became symmetric —
    every assertion below proves nothing.
    """
    repo_a, repo_b = _fixture(tmp_path)
    cmd = ["git", "status", "--porcelain", "--ignored=matching", "--", "tracked.txt"]

    normal = subprocess.run(cmd, cwd=str(repo_b), env=_clean_env(),
                            capture_output=True, text=True, timeout=60)
    leaked_env = _clean_env()
    leaked_env["GIT_DIR"] = str(repo_a / ".git")
    leaked = subprocess.run(cmd, cwd=str(repo_b), env=leaked_env,
                            capture_output=True, text=True, timeout=60)

    assert normal.returncode == 0 and leaked.returncode == 0, (
        "the leak must not be caught by a failure — #705's `git?` decline "
        "cannot see this bug precisely because git exits 0"
    )
    assert normal.stdout[:2] == " M", normal.stdout
    assert leaked.stdout[:2] == "??", (
        "GIT_DIR is not changing the index answer in this fixture", leaked.stdout)


# --------------------------------------------------------------------------
# The post-condition: a built-in read describes THIS repo
# --------------------------------------------------------------------------

def _meta_line(stdout: str) -> str:
    m = re.search(r"^\(\d+ lines, \d+ bytes\)(.*)$", stdout, re.MULTILINE)
    assert m, f"no read meta line in output:\n{stdout}"
    return m.group(1)


def test_read_under_a_leaked_git_dir_describes_the_cwd_repo(tmp_path):
    """The load-bearing invariant, stated as the answer and not as the call.

    ` m` means "differs from the index". Under the leak the same file reads
    ` ?` — untracked — because the index consulted belongs to repoA. Asserting
    that a scrub function ran would be a proxy; this asserts the marker is
    right.
    """
    repo_a, repo_b = _fixture(tmp_path)

    r = _run_op("read:tracked.txt", repo_b, git_dir=repo_a / ".git")

    assert r.returncode == 0, r.stdout + r.stderr
    meta = _meta_line(r.stdout)
    assert "m" in meta.split(), (
        "read described the file using repoA's index", r.stdout)
    assert "?" not in meta.split(), r.stdout


def test_read_with_a_clean_environment_is_unchanged(tmp_path):
    """The same op, no leak — pins that ` m` is the honest answer here."""
    _repo_a, repo_b = _fixture(tmp_path)

    r = _run_op("read:tracked.txt", repo_b)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "m" in _meta_line(r.stdout).split(), r.stdout


def test_workspace_reports_the_branch_of_the_cwd_repo(tmp_path):
    """`workspace` spawns git six more times, none of them through a preset.

    Branch name rather than file state: it can only have come from the leaked
    git dir, so the assertion cannot pass on a coincidence of the work tree.
    """
    repo_a, repo_b = _fixture(tmp_path)

    r = _run_op("workspace:tracked.txt", repo_b, git_dir=repo_a / ".git")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "branch-of-repo-b" in r.stdout, r.stdout
    assert "branch-of-repo-a" not in r.stdout, r.stdout


def test_validate_staged_reads_the_cwd_repos_index(tmp_path):
    """The op the pre-commit hook actually runs, under the env that hook sets.

    Files staged in each repo, distinct names. Under the leak `git diff
    --cached` answers out of repoA's index; repoA's staged path does not exist
    in repoB's work tree, so it is dropped by the outside-cwd filter and the op
    printed `no staged files` — with a file staged. A pre-commit hook that
    validates nothing and exits 0 is the worst shape this bug can take.

    Asserting only the absence of repoA's name would pass on that empty
    answer — it did, in the RED run. The load-bearing half is that repoB's
    staged file IS named.
    """
    repo_a, repo_b = _fixture(tmp_path)
    (repo_a / "only-in-a.py").write_text("a = 1\n")
    _git(["add", "only-in-a.py"], repo_a)
    (repo_b / "only-in-b.py").write_text("b = 1\n")
    _git(["add", "only-in-b.py"], repo_b)

    r = _run_op("validate_staged", repo_b, git_dir=repo_a / ".git")

    assert "only-in-b.py" in r.stdout, r.stdout + r.stderr
    assert "no staged files" not in r.stdout, r.stdout
    assert "only-in-a.py" not in r.stdout, r.stdout


# --------------------------------------------------------------------------
# Loud, not silent — #692's other half, on the built-in side
# --------------------------------------------------------------------------

def test_the_scrub_is_reported_on_a_builtin_op(tmp_path):
    """A silent scrub tells a deliberate caller nothing. Same rule either side."""
    repo_a, repo_b = _fixture(tmp_path)

    r = _run_op("read:tracked.txt", repo_b, git_dir=repo_a / ".git")

    assert "scrubbed inherited git env" in r.stdout, r.stdout + r.stderr
    assert "GIT_DIR" in r.stdout


def test_a_clean_environment_produces_no_notice_on_a_builtin_op(tmp_path):
    """No leak, no line — the notice has to mean something when it appears."""
    _repo_a, repo_b = _fixture(tmp_path)

    r = _run_op("read:tracked.txt", repo_b)

    assert "scrubbed inherited git env" not in r.stdout, r.stdout


def test_the_notice_is_said_once_per_call_not_once_per_op(tmp_path):
    """Run-level scrub, run-level notice. Six ops must not print six lines."""
    repo_a, repo_b = _fixture(tmp_path)

    env = _clean_env()
    env["GIT_DIR"] = str(repo_a / ".git")
    r = subprocess.run(
        [sys.executable, str(SUPERTOOL),
         "read:tracked.txt", "read:b.txt", "read:.supertool.json"],
        cwd=str(repo_b), capture_output=True, text=True, timeout=180, env=env,
    )

    assert r.stdout.count("scrubbed inherited git env") == 1, r.stdout


# --------------------------------------------------------------------------
# The design pin — one boundary, and a named one
# --------------------------------------------------------------------------

def test_the_scrub_has_exactly_one_call_site_and_it_is_the_launcher():
    """#692 argued for one chokepoint; a second scrub site invites a third.

    This issue exists because the chosen chokepoint was one level too low —
    the *preset* launcher rather than the *process* launcher. The fix moves it
    up, it does not add to it. Pinned so the next `GIT_*` bug is fixed by
    widening this boundary rather than by sprinkling a third call.

    A proxy assertion, and labelled as one: the behavioural tests above are the
    post-condition. This one guards the shape of the answer, not the answer.
    """
    src = SUPERTOOL.read_text(encoding="utf-8")
    call_lines = [
        i + 1 for i, line in enumerate(src.splitlines())
        if "scrub_git_env(" in line and not line.lstrip().startswith("def ")
    ]
    assert len(call_lines) == 1, (
        f"scrub_git_env is called from {len(call_lines)} places "
        f"(lines {call_lines}) — the point of #714 is that there is one"
    )

    # …and that the one place is the process launcher, so an op is covered by
    # being dispatched rather than by remembering to opt in.
    enclosing = None
    for line in src.splitlines()[:call_lines[0]]:
        if line.startswith("def "):
            enclosing = line.split("(")[0][4:]
    assert enclosing == "_main", (
        f"the scrub sits in {enclosing!r}; #714 puts it at the launcher")
