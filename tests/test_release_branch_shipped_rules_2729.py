"""#2729 -- the release tree must not ship the one shipped jit-context guard
rule disabled with nothing telling the caller.

`.github/release-branch.json` denies the whole `.claude/` tree. That is also
where `hooks/shipped_rules.py` reads, at runtime, the index and body of the
one shipped jit-context rule (`supertool-no-cut.md`). A release build with
the deny-list alone therefore ships the guard loaded nowhere, silently --
`check_release_tree.py`'s pre-submission checklist does not look at it and
`smoke_release_tree.py` only asked for exit 0 from a hook that always
returns 0 by design.

The fix is an exact-path carve-out (`keep` in the config, `is_kept()` in
`build_release_tree.py`) that wins over the `.claude/` deny prefix for only
the two files `shipped_rules.py` reads, plus a `smoke_release_tree.py` check
that fails the build when `hooks/guard-selftest.py` reports a shipped rule
"not loaded" in the tree it just built.

Every "must be removed" assertion here is paired with a "must be kept" one,
and the pre-fix (bare deny-list) state is reproduced directly rather than
asserted only in a commit message no test will ever re-check.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"
SMOKE = REPO_ROOT / ".github" / "scripts" / "smoke_release_tree.py"
CONFIG = REPO_ROOT / ".github" / "release-branch.json"

posix_only = pytest.mark.skipif(os.name == "nt", reason="hook commands are bash")

RULE_DIR_REL = ".claude/jit-context/tools/00-manual"
INDEX_REL = RULE_DIR_REL + "/00-index.tsv"
RULE_BODY_REL = RULE_DIR_REL + "/supertool-no-cut.md"

INDEX_ROW = "Bash\t~stand-in-pattern-does-not-matter\tsupertool-no-cut.md\tblock\t\t\t\n"
RULE_BODY_TEXT = "---\ntitle: stand-in no-cut rule\ntool: Bash\nmatch: ~x\nmode: block\n---\n\nbody\n"


def _load(path: Path, name: str):
    assert path.exists(), f"{path} does not exist (#2705)"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, env=_git_env())


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "src"
    files = {
        ".claude-plugin/plugin.json": '{"name": "example", "version": "0.1.0"}',
        "hooks/hooks.json": '{"hooks": {}}',
        "hooks/shipped_rules.py": (REPO_ROOT / "hooks" / "shipped_rules.py")
            .read_text(encoding="utf-8"),
        "hooks/guard-selftest.py": (REPO_ROOT / "hooks" / "guard-selftest.py")
            .read_text(encoding="utf-8"),
        "LICENSE": "license\n",
        "README.md": "# example\n" + "word " * 40 + "\n",
        INDEX_REL: INDEX_ROW,
        RULE_BODY_REL: RULE_BODY_TEXT,
        ".claude/other.md": "not shipped, must still be denied\n",
    }
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=_git_env())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "v0.1.0")
    return repo


def _config(**over) -> dict:
    cfg = {
        "repo": "Example-Org/example",
        "default_branch": "main",
        "deny": [".claude/"],
        "keep": [INDEX_REL, RULE_BODY_REL],
    }
    cfg.update(over)
    return cfg


def _inventory_lines(out: Path, tmp_path: Path, name: str) -> list:
    """shipped_rules.inventory(out, tmp_path), the same call
    hooks/guard-selftest.py's rule_inventory() makes -- imported directly
    rather than run as the full guard-selftest.py subprocess, which also
    exercises the unrelated raw-command `replaces` registry and needs the
    whole plugin's presets to answer that half. A synthetic fixture only
    needs hooks/shipped_rules.py itself; the real-tree test below runs the
    actual guard-selftest.py, where that whole registry does exist."""
    mod = _load(out / "hooks" / "shipped_rules.py", name)
    return mod.inventory(str(out), str(tmp_path))


def _run_guard_selftest(out: Path, tmp_path: Path):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(out)
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, str(out / "hooks" / "guard-selftest.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, check=False, timeout=60,
    )


# -- the carve-out itself ---------------------------------------------------

def test_without_a_keep_list_the_shipped_rule_files_are_denied_too(tmp_path):
    """Red: reproduces #2729 on a bare deny-list, no carve-out."""
    mod = _load(BUILD, "build_release_tree_2729_a")
    repo = _make_repo(tmp_path)
    out = tmp_path / "out"
    mod.build(repo, "v0.1.0", out, _config(keep=[]))
    assert not (out / INDEX_REL).exists()
    assert not (out / RULE_BODY_REL).exists()


def test_the_keep_list_carves_out_exactly_the_two_files_the_guard_reads(tmp_path):
    mod = _load(BUILD, "build_release_tree_2729_b")
    repo = _make_repo(tmp_path)
    out = tmp_path / "out"
    mod.build(repo, "v0.1.0", out, _config())
    assert (out / INDEX_REL).is_file()
    assert (out / RULE_BODY_REL).is_file()
    # Negative control: a sibling file under the same denied prefix, not
    # named by `keep`, still does not ship.
    assert not (out / ".claude" / "other.md").exists()


def test_a_keep_entry_is_exact_not_a_prefix(tmp_path):
    """`keep` carve-outs are exact paths; a sibling under the same directory
    is not swept in just because it shares a prefix with a kept file."""
    mod = _load(BUILD, "build_release_tree_2729_prefix")
    repo = _make_repo(tmp_path)
    (repo / RULE_DIR_REL / "supertool-no-cut.md.bak").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "extra")
    _git(repo, "tag", "-f", "v0.1.0")
    out = tmp_path / "out"
    mod.build(repo, "v0.1.0", out, _config())
    assert (out / RULE_BODY_REL).is_file()
    assert not (out / RULE_DIR_REL / "supertool-no-cut.md.bak").exists()


# -- guard-selftest sees the result -----------------------------------------

def test_guard_selftest_reports_the_rule_enforcing_once_carved_out(tmp_path):
    mod = _load(BUILD, "build_release_tree_2729_c")
    repo = _make_repo(tmp_path)
    out = tmp_path / "out"
    mod.build(repo, "v0.1.0", out, _config())
    lines = _inventory_lines(out, tmp_path, "shipped_rules_2729_c")
    assert any("supertool-no-cut.md : enforcing as deny" in l for l in lines), lines
    assert not any("not loaded" in l for l in lines), lines


def test_guard_selftest_reports_not_loaded_without_the_keep_list(tmp_path):
    """Negative control for the one above: on the pre-fix deny-list alone,
    the rule is reported as not loaded, not silently absent from the
    report -- this is the exact line #2729 says nothing ever surfaced."""
    mod = _load(BUILD, "build_release_tree_2729_d")
    repo = _make_repo(tmp_path)
    out = tmp_path / "out"
    mod.build(repo, "v0.1.0", out, _config(keep=[]))
    lines = _inventory_lines(out, tmp_path, "shipped_rules_2729_d")
    assert any("supertool-no-cut.md : not loaded" in l for l in lines), lines


# -- the real config and the real tree --------------------------------------

def test_the_real_config_carves_out_the_two_files_the_guard_reads():
    mod = _load(BUILD, "build_release_tree_2729_real_cfg")
    cfg = mod.load_config(CONFIG)
    keep = set(cfg.get("keep", []))
    assert INDEX_REL in keep
    assert RULE_BODY_REL in keep
    # Positive control: the carve-out is scoped to exactly these two files,
    # not the whole directory -- a prefix entry here would defeat the point.
    for entry in keep:
        assert not entry.endswith("/"), entry


def test_building_this_repository_head_ships_the_two_shipped_rule_files(tmp_path):
    """Integration: the real deny-list, with its keep carve-out, against the
    real tree -- same pattern as
    test_building_this_repository_head_ships_every_hook_script in
    test_release_branch_build_2705.py."""
    mod = _load(BUILD, "build_release_tree_2729_head")
    cfg = mod.load_config(CONFIG)
    out = tmp_path / "out"
    mod.build(REPO_ROOT, "HEAD", out, cfg)
    assert (out / INDEX_REL).is_file()
    assert (out / RULE_BODY_REL).is_file()
    r = _run_guard_selftest(out, tmp_path)
    assert "supertool-no-cut.md : enforcing as deny" in r.stdout, r.stdout
    assert "not loaded" not in r.stdout


# -- smoke_release_tree.py's own positive control ---------------------------
#
# The tests above all run hooks/guard-selftest.py as a standalone subprocess
# and read ITS stdout. None of them calls smoke_release_tree.check_shipped_
# rules() itself -- the function this issue actually adds to make a release
# BUILD fail -- so a bug in that function's own string-matching could pass
# every test above while still misreporting the build (found in review,
# #2729: the first cut only matched "not loaded" and missed guard-selftest's
# other failure shape, "rules       : could not run" when hooks/shipped_
# rules.py itself cannot be imported, plus a third case neither matched --
# the subprocess producing no output at all).

def _stub_selftest(body: str) -> dict:
    """A one-command hooks.json plus a stand-in hooks/guard-selftest.py and
    hooks/pre-bash-guard.sh (smoke_release_tree.py needs hooks.json to run
    at all; PreToolUse is this plugin's own minimal hook list, #2705)."""
    return {
        "hooks/hooks.json": json.dumps({"hooks": {"PreToolUse": [
            {"hooks": [{"type": "command",
                        "command": 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/noop.sh"'}]}]}}),
        "hooks/noop.sh": "#!/bin/sh\nexit 0\n",
        "hooks/guard-selftest.py": body,
    }


def _write_tree(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "tree"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


@posix_only
def test_smoke_fails_when_guard_selftest_reports_not_loaded(tmp_path):
    mod = _load(SMOKE, "smoke_release_tree_2729_a")
    tree = _write_tree(tmp_path, _stub_selftest(
        "import sys\nprint('  supertool-no-cut.md : not loaded')\nsys.exit(0)\n"))
    result = mod.run_smoke(tree, validate="skip", linger_seconds=5)
    assert not result.ok, result.report()
    assert "not loaded" in result.report()


@posix_only
def test_smoke_passes_when_guard_selftest_reports_enforcing(tmp_path):
    """Positive control for the one above: the same harness, a clean report."""
    mod = _load(SMOKE, "smoke_release_tree_2729_b")
    tree = _write_tree(tmp_path, _stub_selftest(
        "import sys\nprint('  supertool-no-cut.md : enforcing as deny')\nsys.exit(0)\n"))
    result = mod.run_smoke(tree, validate="skip", linger_seconds=5)
    assert result.ok, result.report()


@posix_only
def test_smoke_fails_when_shipped_rules_cannot_be_imported(tmp_path):
    """guard-selftest.py's OTHER failure shape for this layer: hooks/shipped_
    rules.py itself missing or broken in the built tree. No "not loaded"
    substring anywhere in this line -- the gap found in review."""
    mod = _load(SMOKE, "smoke_release_tree_2729_c")
    tree = _write_tree(tmp_path, _stub_selftest(
        "import sys\n"
        "print('  rules       : could not run - hooks/shipped_rules.py could "
        "not be imported from X (No module named shipped_rules)')\n"
        "sys.exit(0)\n"))
    result = mod.run_smoke(tree, validate="skip", linger_seconds=5)
    assert not result.ok, result.report()
    assert "could not run" in result.report()


@posix_only
def test_smoke_fails_when_guard_selftest_produces_no_output(tmp_path):
    """A crash before the first print leaves nothing for either substring
    check to match -- the exact absence-read-as-presence shape #2729 is
    about, one layer up (found in review)."""
    mod = _load(SMOKE, "smoke_release_tree_2729_d")
    tree = _write_tree(tmp_path, _stub_selftest("import sys\nsys.exit(1)\n"))
    result = mod.run_smoke(tree, validate="skip", linger_seconds=5)
    assert not result.ok, result.report()
    assert "no output at all" in result.report()


@posix_only
def test_smoke_skips_the_check_when_guard_selftest_is_absent(tmp_path):
    """A fork of this build script for a plugin with no shipped rules of its
    own ships no hooks/guard-selftest.py at all -- that is a different state
    from one that has the file and the rule not loading, and must not fail."""
    mod = _load(SMOKE, "smoke_release_tree_2729_e")
    files = _stub_selftest("")
    del files["hooks/guard-selftest.py"]
    tree = _write_tree(tmp_path, files)
    result = mod.run_smoke(tree, validate="skip", linger_seconds=5)
    assert result.ok, result.report()
    assert "not in this tree, skipped" in result.report()
