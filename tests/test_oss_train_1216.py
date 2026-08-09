"""`oss_train` answers from this checkout, and its two #910 renders are honest.

The op was a DVSI project op (`.claude/scripts/oss_train.py` plus a block in that
project's `.supertool.json`), so a session rooted in claude-supertool could not
run the maintainer loop at all — `unknown operation: oss_train` (#1216). It is
ported here unchanged in behaviour, with the two defects filed as #910 fixed:

* `dry` used to rebase and skip only the push while three surfaces said DRY RUN.
  Those branches are checked out in worktrees where agents work, so the
  safe-sounding flag moved HEAD underneath live work. `dry` now stops ABOVE the
  rebase, and `test_dry_leaves_the_branch_exactly_where_it_found_it` is the pin:
  it fails the moment the stop moves back below.
* each branch was labelled by its worktree DIRECTORY. `st-wt/749` rendered as
  `fix/749` for a tree holding `lane-watch`, and every follow-up command a reader
  would run takes a branch name.

What is deliberately NOT covered, because covering it means force-pushing real
refs: the PUSHED path, the REFUSED path (which needs `git-resolve` to decline
inside a real supertool checkout), and `rebase --continue` / `--abort`. Every
test below stops at or before the rebase, and none of them has a remote outside
`tmp_path`. Saying so is worth more than a fixture that pretends.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "oss_train.py"


def _load_oss_train():
    spec = importlib.util.spec_from_file_location("oss_train_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None, SCRIPT
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oss_train = _load_oss_train()


# ---------------------------------------------------------------------------
# fixtures — every remote lives inside tmp_path, nothing is ever pushed
# ---------------------------------------------------------------------------

def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stdout}{proc.stderr}"
    return proc.stdout


def _commit(clone: Path, name: str, body: str) -> None:
    (clone / name).write_text(body, encoding="utf-8")
    _git("add", name, cwd=clone)
    _git("-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-q", "-m", name, cwd=clone)


@pytest.fixture()
def train_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A clone whose branch `feature/xyz` sits in a worktree named `999`.

    The worktree DIRECTORY and the BRANCH deliberately disagree: that
    disagreement is the whole of the second #910 defect, and it is invisible
    whenever the fix/NNN convention holds.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)],
                   check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", origin.as_posix(), str(clone)],
                   check=True, capture_output=True)

    _commit(clone, "base.txt", "base")
    _git("push", "-q", "origin", "master", cwd=clone)
    _git("checkout", "-q", "-b", "feature/xyz", cwd=clone)
    _commit(clone, "feature.txt", "feature")
    _git("checkout", "-q", "master", cwd=clone)
    _commit(clone, "later.txt", "later")
    _git("push", "-q", "origin", "master", cwd=clone)

    wt_root = tmp_path / "st-wt"
    wt = wt_root / "999"
    _git("worktree", "add", "-q", str(wt), "feature/xyz", cwd=clone)

    monkeypatch.setenv("SUPERTOOL_WT_ROOT", str(wt_root))
    return {"origin": origin, "clone": clone, "wt_root": wt_root, "wt": wt}


def _run_main(monkeypatch: pytest.MonkeyPatch, arg: str) -> int:
    monkeypatch.setattr(sys, "argv", ["oss_train.py", arg])
    return oss_train.main()


# ---------------------------------------------------------------------------
# the flag arrives as a COMMA element
# ---------------------------------------------------------------------------

class TestArgumentParsing:
    """Only the first ':'-token reaches a project op's {file}; the rest is
    discarded silently, so `all:dry` never carries the flag anywhere."""

    @pytest.mark.parametrize("raw, arg, dry", [
        ("all,dry", "all", True),
        ("dry", "", True),
        ("862,860,dry", "862,860", True),
        ("all", "all", False),
        ("862,860,861", "862,860,861", False),
        (" all , dry ", "all", True),
        ("", "", False),
    ])
    def test_comma_form(self, raw: str, arg: str, dry: bool) -> None:
        assert oss_train.parse_tokens([raw]) == (arg, dry)

    def test_a_colon_that_did_survive_is_still_read(self) -> None:
        """Defensive, not a supported form: if a colon ever does arrive intact,
        reading it as a separator is strictly safer than treating `all:dry` as
        an unknown target and running the un-dry path."""
        assert oss_train.parse_tokens(["all:dry"]) == ("all", True)


# ---------------------------------------------------------------------------
# a bare invocation is refused, and names the live count
# ---------------------------------------------------------------------------

def test_a_bare_invocation_refuses_and_counts_what_it_would_have_touched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    wt_root = tmp_path / "st-wt"
    for name in ("101", "202", "notanumber"):
        (wt_root / name).mkdir(parents=True)
    monkeypatch.setenv("SUPERTOOL_WT_ROOT", str(wt_root))

    assert _run_main(monkeypatch, "") == 2
    out = capsys.readouterr().out
    assert "needs an explicit target" in out
    assert "2 branch(es)" in out, out
    assert "101, 202" in out
    assert "notanumber" not in out


def test_discover_lists_only_numeric_worktrees(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wt_root = tmp_path / "st-wt"
    for name in ("7", "12", "lane-watch"):
        (wt_root / name).mkdir(parents=True)
    (wt_root / "88").write_text("a file, not a worktree", encoding="utf-8")
    monkeypatch.setenv("SUPERTOOL_WT_ROOT", str(wt_root))
    assert oss_train.discover() == ["12", "7"]


def test_a_missing_worktree_is_FAILED_and_exits_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("SUPERTOOL_WT_ROOT", str(tmp_path / "st-wt"))
    assert _run_main(monkeypatch, "424242") == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "[result] FAILED: 1" in out


# ---------------------------------------------------------------------------
# #910 (1) — dry is read-only
# ---------------------------------------------------------------------------

def test_dry_leaves_the_branch_exactly_where_it_found_it(
        train_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    wt = train_world["wt"]
    before = _git("rev-parse", "HEAD", cwd=wt).strip()

    assert _run_main(monkeypatch, "999,dry") == 0

    after = _git("rev-parse", "HEAD", cwd=wt).strip()
    assert after == before, (
        "dry rebased the branch. It stops ABOVE the rebase precisely because "
        "these worktrees hold live agents (#910)")
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "DRY" in out
    assert "1 commit(s) behind" in out, out


def test_dry_pushes_nothing(train_world, monkeypatch: pytest.MonkeyPatch) -> None:
    _run_main(monkeypatch, "999,dry")
    refs = _git("ls-remote", "--heads", train_world["origin"].as_posix(),
                cwd=train_world["clone"])
    assert "feature/xyz" not in refs, refs


def test_dry_does_not_leave_a_rebase_in_progress(
        train_world, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rebase that started and stopped is not 'nothing was touched' either."""
    _run_main(monkeypatch, "999,dry")
    git_dir = Path(_git("rev-parse", "--absolute-git-dir",
                        cwd=train_world["wt"]).strip())
    assert not (git_dir / "rebase-merge").exists()
    assert not (git_dir / "rebase-apply").exists()


# ---------------------------------------------------------------------------
# #910 (2) — the label is the branch git reports
# ---------------------------------------------------------------------------

def test_the_label_is_the_branch_not_the_directory(
        train_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_main(monkeypatch, "999,dry") == 0
    out = capsys.readouterr().out
    assert "feature/xyz" in out, out
    assert "fix/999" not in out, out


def test_a_dirty_worktree_is_BUSY_and_is_still_named_by_its_branch(
        train_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """BUSY returns before the fetch, so this exercises the label on the one
    path that touches no remote at all."""
    (train_world["wt"] / "agent-scratch.txt").write_text("mid-task", encoding="utf-8")

    assert _run_main(monkeypatch, "999") == 0
    out = capsys.readouterr().out
    assert "BUSY" in out
    assert "feature/xyz" in out, out
    assert "fix/999" not in out, out
    assert "someone is working here" in out


def test_a_detached_worktree_is_FAILED_rather_than_guessed_at(
        train_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    wt = train_world["wt"]
    _git("checkout", "-q", "--detach", "HEAD", cwd=wt)

    assert _run_main(monkeypatch, "999") == 1
    out = capsys.readouterr().out
    assert "detached HEAD" in out
    assert "no branch" in out


# ---------------------------------------------------------------------------
# registration — the op has to actually answer from this checkout
# ---------------------------------------------------------------------------

class TestRegistration:

    @staticmethod
    def _entry() -> dict:
        config = json.loads((REPO / ".supertool.json").read_text(encoding="utf-8"))
        assert "oss_train" in config["ops"], (
            "the whole point of #1216 is that this op answers from "
            "claude-supertool; an unregistered script answers from nowhere")
        return config["ops"]["oss_train"]

    def test_the_cmd_points_at_a_file_that_exists(self) -> None:
        cmd = self._entry()["cmd"]
        assert "scripts/oss_train.py" in cmd, cmd
        assert SCRIPT.is_file(), SCRIPT

    def test_it_runs_the_interpreter_supertool_is_running(self) -> None:
        """`{python}` rather than a literal `python3`: the placeholder resolves
        to the running interpreter, which is the only one guaranteed present."""
        assert "{python}" in self._entry()["cmd"]

    def test_the_worktree_root_is_configuration_not_a_constant(self) -> None:
        """Extra config keys arrive as SUPERTOOL_<KEY> env vars, which is what
        lets every test above point the op at a tmp_path instead of at the
        machine's real worktrees."""
        assert self._entry()["wt_root"] == "~/Documents/st-wt"

    def test_the_timeout_outlives_a_real_train(self) -> None:
        assert self._entry().get("timeout", 60) >= 600

    def test_the_description_does_not_promise_the_colon_form(self) -> None:
        """The description shipped in DVSI said 'append :dry', which is the one
        form measured NOT to arrive."""
        description = self._entry()["description"]
        assert ":dry" not in description, description
        assert ",dry" in description, description


def test_the_script_is_not_packaged_into_the_wheel() -> None:
    """A maintainer op that reads ~/Documents/st-wt is not part of the tool."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'py-modules = ["supertool", "_supertool"]' in pyproject


def test_the_coverage_gate_accounts_for_the_new_directory() -> None:
    """`docs/validators.md` §"Declining instead of guessing": an unmeasured
    directory that nothing names reads exactly like one that passed."""
    sys.path.insert(0, str(REPO / ".github" / "scripts"))
    try:
        import coverage_gate
    finally:
        sys.path.pop(0)
    named = (set(coverage_gate.ENFORCED)
             | set(coverage_gate.MEASURED_NOT_ENFORCED)
             | set(coverage_gate.NOT_MEASURED_PY))
    assert "scripts/" in named, sorted(named)


def test_the_module_imports_without_touching_the_machine(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Import must not expand or stat the real worktree root: the tests point
    it elsewhere by env var AFTER import."""
    monkeypatch.setenv("SUPERTOOL_WT_ROOT", os.path.join("nowhere", "at", "all"))
    assert oss_train.wt_root().endswith(os.path.join("nowhere", "at", "all"))
