"""#2263 -- a scoped pre-push runner for the env-scrub and splitlines-register
meta-guards (encoding-seam is already covered by #2288's own script; this
umbrella calls into it rather than re-implementing it).

Each guard here is a tree-wide scanner keyed on a *pattern*, not on a lane's
own file list, so it never showed up in a targeted TDD/self-review loop --
only a full pytest run or CI would trip it. `.github/scripts/check_meta_guards.py`
runs the SAME scan functions the real guards use, imported rather than
duplicated, scoped to an explicit file list (in CI: `git diff`).

Every fixture below runs entirely under `tmp_path`, never inside this repo's
own `tests/`/`presets/` trees: `tests/_write_guard.py` (#1998) forbids a test
writing a throwaway `.py` file inside `repo_python_files()`'s walk root, on
pain of the exact #1981 race this suite already paid for once. The real guard
modules (`tests/_pathenv_scan.py`, the splitlines register) are copied --
never re-written -- into the `tmp_path` tree so the functions under test load
the genuine scan logic while every file they scope to lives outside the
guarded root.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "scripts" / "check_meta_guards.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_meta_guards_2263", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(tmp_path: Path, relpath: str, content: str) -> Path:
    p = tmp_path / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _with_env_scrub_module(tmp_path: Path) -> None:
    """Copy (never rewrite) the real guard module into the sandbox root."""
    dest = tmp_path / "tests" / "_pathenv_scan.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "tests" / "_pathenv_scan.py", dest)


def _with_splitlines_register_module(tmp_path: Path) -> None:
    dest = (tmp_path / "tests"
            / "test_preset_git_splitlines_register_1130.py")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        REPO / "tests" / "test_preset_git_splitlines_register_1130.py", dest)


# ---------------------------------------------------------------------------
# env-scrub
# ---------------------------------------------------------------------------

def test_env_scrub_catches_a_path_only_dict_at_a_python_spawn(tmp_path) -> None:
    mod = _load()
    _with_env_scrub_module(tmp_path)
    _write(tmp_path, "tests/test_sample_offender.py",
           "import subprocess, sys\n"
           "def f():\n"
           "    subprocess.run([sys.executable, '-c', 'pass'], env={'PATH': ''})\n")
    findings = mod.check_env_scrub(tmp_path, ["tests/test_sample_offender.py"])
    assert findings is not None
    kinds = [f.kind for f in findings]
    assert "violation" in kinds, [f.describe() for f in findings]


def test_env_scrub_is_quiet_on_the_helper_it_exists_to_require(tmp_path) -> None:
    """Positive control: the fix this guard asks for must not itself be flagged."""
    mod = _load()
    _with_env_scrub_module(tmp_path)
    _write(tmp_path, "tests/test_sample_clean.py",
           "import subprocess, sys\n"
           "import _winenv\n"
           "def f():\n"
           "    subprocess.run([sys.executable, '-c', 'pass'],\n"
           "                   env=_winenv.empty_path_env())\n")
    findings = mod.check_env_scrub(tmp_path, ["tests/test_sample_clean.py"])
    assert findings is not None
    assert findings == [], [f.describe() for f in findings]


def test_env_scrub_ignores_a_file_outside_its_own_population(tmp_path) -> None:
    """`scan_tree` only ever looks at tests/test_*.py; a presets/ file with
    the identical offending shape is not this check's business."""
    mod = _load()
    _with_env_scrub_module(tmp_path)
    _write(tmp_path, "presets/git/sample.py",
           "import subprocess, sys\n"
           "def f():\n"
           "    subprocess.run([sys.executable, '-c', 'pass'], env={'PATH': ''})\n")
    findings = mod.check_env_scrub(tmp_path, ["presets/git/sample.py"])
    assert findings == []


def test_env_scrub_reports_none_when_not_adopted(tmp_path) -> None:
    """Absence of the convention is `None` (skipped), never a silent pass
    folded into the same shape as 'checked and clean'."""
    mod = _load()
    findings = mod.check_env_scrub(tmp_path, ["tests/whatever.py"])
    assert findings is None


# ---------------------------------------------------------------------------
# splitlines register
# ---------------------------------------------------------------------------

def test_splitlines_register_flags_a_new_unregistered_call_site(tmp_path) -> None:
    mod = _load()
    _with_splitlines_register_module(tmp_path)
    _write(tmp_path, "presets/git/sample.py",
           "def _reader(out):\n"
           "    return out.splitlines()\n")
    offenders = mod.check_splitlines_register(tmp_path, ["presets/git/sample.py"])
    assert offenders is not None
    assert any(o[0] == "presets/git/sample.py" for o in offenders), offenders


def test_splitlines_register_is_quiet_on_a_registered_call_site(tmp_path) -> None:
    """Positive control: a real REGISTER entry must not fire from this scoped
    runner just because its file is among the ones scanned."""
    mod = _load()
    _with_splitlines_register_module(tmp_path)
    real_module = mod._load_module_from(
        REPO, "tests/test_preset_git_splitlines_register_1130.py",
        "meta_guard_splitlines_register_probe")
    assert real_module is not None
    registered_key = next(iter(real_module.REGISTER))
    relpath, _func = registered_key.split("::", 1)
    src = REPO / relpath
    assert src.is_file(), relpath
    dest = tmp_path / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)

    offenders = mod.check_splitlines_register(tmp_path, [relpath])
    flagged_keys = {o[1] for o in (offenders or [])}
    assert registered_key not in flagged_keys, offenders


def test_splitlines_register_ignores_a_file_outside_presets_git(tmp_path) -> None:
    mod = _load()
    _with_splitlines_register_module(tmp_path)
    _write(tmp_path, "presets/github/sample.py",
           "def _reader(out):\n"
           "    return out.splitlines()\n")
    offenders = mod.check_splitlines_register(tmp_path, ["presets/github/sample.py"])
    assert offenders == []


def test_splitlines_register_reports_none_when_not_adopted(tmp_path) -> None:
    mod = _load()
    offenders = mod.check_splitlines_register(tmp_path, ["presets/git/x.py"])
    assert offenders is None


# ---------------------------------------------------------------------------
# The umbrella CLI -- explicit files, three states on the exit code
# ---------------------------------------------------------------------------

def _sandboxed_repo(tmp_path: Path) -> Path:
    """A `tmp_path` standing in for the repo root, with the real guard
    modules copied in and `.git` present so `_ces.repo_root` resolves it."""
    (tmp_path / ".git").mkdir()
    _with_env_scrub_module(tmp_path)
    _with_splitlines_register_module(tmp_path)
    return tmp_path


def test_main_exits_clean_on_a_harmless_change(tmp_path, monkeypatch, capsys) -> None:
    mod = _load()
    root = _sandboxed_repo(tmp_path)
    monkeypatch.setattr(mod._ces, "repo_root", lambda _cwd: root)
    scratch = _write(root, "presets/git/harmless.py",
                      "def f(x):\n    return x + 1\n")
    rc = mod.main([str(scratch)])
    assert rc == mod.RC_OK
    out = capsys.readouterr().out
    assert "clean" in out


def test_main_exits_nonzero_on_a_real_violation(tmp_path, monkeypatch, capsys) -> None:
    mod = _load()
    root = _sandboxed_repo(tmp_path)
    monkeypatch.setattr(mod._ces, "repo_root", lambda _cwd: root)
    scratch = _write(root, "tests/test_offender_main.py",
                      "import subprocess, sys\n"
                      "def f():\n"
                      "    subprocess.run([sys.executable, '-c', 'pass'], "
                      "env={'PATH': ''})\n")
    rc = mod.main([str(scratch)])
    assert rc == mod.RC_VIOLATIONS
    out = capsys.readouterr().out
    assert "env-scrub" in out


def test_main_reports_could_not_check_when_no_guard_is_adopted(
        tmp_path, monkeypatch, capsys) -> None:
    """A sandbox with none of the three guard files adopted must say so on
    the exit code (RC_COULD_NOT_CHECK), never silently RC_OK."""
    mod = _load()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(mod._ces, "repo_root", lambda _cwd: tmp_path)
    scratch = _write(tmp_path, "presets/git/harmless.py",
                      "def f(x):\n    return x + 1\n")
    rc = mod.main([str(scratch)])
    assert rc == mod.RC_COULD_NOT_CHECK


def _with_encoding_seam_module(tmp_path: Path) -> None:
    dest = tmp_path / "tests" / "test_encoding_seam.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "tests" / "test_encoding_seam.py", dest)


# ---------------------------------------------------------------------------
# _load_module_from -- absence and failure, not just success
# ---------------------------------------------------------------------------

def test_load_module_from_returns_none_for_a_missing_file(tmp_path) -> None:
    mod = _load()
    assert mod._load_module_from(tmp_path, "tests/nope.py", "x") is None


def test_load_module_from_returns_none_when_the_file_cannot_execute(tmp_path) -> None:
    mod = _load()
    _write(tmp_path, "tests/_pathenv_scan.py", "raise RuntimeError('boom')\n")
    assert mod._load_module_from(tmp_path, "tests/_pathenv_scan.py", "x") is None


# ---------------------------------------------------------------------------
# env-scrub -- the unresolved arm, not only violation/clean
# ---------------------------------------------------------------------------

def test_env_scrub_reports_an_unresolved_expression(tmp_path) -> None:
    """A call the scanner has no `return` for anywhere in the file resolves
    to `unresolved`, not `ok` -- `_classify` only reads a helper's own
    `return`, and `f` here has none."""
    mod = _load()
    _with_env_scrub_module(tmp_path)
    _write(tmp_path, "tests/test_sample_unresolved.py",
           "import subprocess, sys\n"
           "def f():\n"
           "    subprocess.run([sys.executable, '-c', 'pass'], env=make_it())\n")
    findings = mod.check_env_scrub(tmp_path, ["tests/test_sample_unresolved.py"])
    assert findings is not None
    kinds = [f.kind for f in findings]
    assert "unresolved" in kinds, [f.describe() for f in findings]


# ---------------------------------------------------------------------------
# splitlines register -- a file the scanner cannot even parse
# ---------------------------------------------------------------------------

def test_splitlines_register_reports_a_syntax_error_as_unreadable(tmp_path) -> None:
    mod = _load()
    _with_splitlines_register_module(tmp_path)
    _write(tmp_path, "presets/git/broken.py", "def f(:\n    pass\n")
    offenders = mod.check_splitlines_register(tmp_path, ["presets/git/broken.py"])
    assert offenders is not None
    assert any(o[1] == "<unreadable>" for o in offenders), offenders


# ---------------------------------------------------------------------------
# encoding-seam -- delegated, but exercised directly too
# ---------------------------------------------------------------------------

def test_encoding_seam_catches_a_real_violation(tmp_path) -> None:
    mod = _load()
    _with_encoding_seam_module(tmp_path)
    _write(tmp_path, "presets/git/offender.py",
           "import subprocess\n"
           "def f():\n"
           "    subprocess.run(['echo', 'hi'], text=True)\n")
    records = mod.check_encoding_seam(tmp_path, ["presets/git/offender.py"])
    assert records is not None
    assert any(r["severity"] == "error" for _p, r in records), records


def test_encoding_seam_reports_none_when_not_adopted(tmp_path) -> None:
    mod = _load()
    records = mod.check_encoding_seam(tmp_path, ["presets/git/whatever.py"])
    assert records is None


# ---------------------------------------------------------------------------
# main() -- error paths and the real git-diff path, not only explicit files
# ---------------------------------------------------------------------------

def test_main_reports_could_not_check_outside_a_git_repo(monkeypatch, capsys) -> None:
    mod = _load()
    monkeypatch.setattr(mod._ces, "repo_root", lambda _cwd: None)
    rc = mod.main([])
    assert rc == mod.RC_COULD_NOT_CHECK
    assert "not inside a git repository" in capsys.readouterr().err


def test_main_declines_an_explicit_file_outside_the_repo_root(
        tmp_path, monkeypatch, capsys) -> None:
    mod = _load()
    root = _sandboxed_repo(tmp_path)
    monkeypatch.setattr(mod._ces, "repo_root", lambda _cwd: root)
    outside = tmp_path.parent / "outside_2263.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    try:
        rc = mod.main([str(outside)])
    finally:
        outside.unlink()
    assert rc == mod.RC_COULD_NOT_CHECK
    assert "outside the repo root" in capsys.readouterr().err


def test_main_declines_when_no_merge_base_is_resolvable(
        tmp_path, monkeypatch, capsys) -> None:
    """No `--base`, no `origin/<branch>` at all -- `_merge_base` fails, and
    that must render as `could not check`, never as a quiet zero-file clean."""
    mod = _load()
    root = _sandboxed_repo(tmp_path)
    monkeypatch.setattr(mod._ces, "repo_root", lambda _cwd: root)
    rc = mod.main([])
    assert rc == mod.RC_COULD_NOT_CHECK
    assert "merge-base" in capsys.readouterr().err


def test_main_runs_the_real_git_diff_path_with_an_explicit_base(capsys) -> None:
    """End to end against THIS repo's own history: diff two real commits,
    which exercises `_changed_files` and the whole aggregation loop rather
    than the explicit-file shortcut every other main() test takes."""
    mod = _load()
    r = subprocess.run(
        ["git", "-C", str(REPO), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, timeout=15,
        encoding="utf-8", errors="replace")
    root_commit = (r.stdout or "").strip().splitlines()
    assert root_commit, "could not find a root commit to diff against"
    old_cwd = Path.cwd()
    try:
        os.chdir(REPO)
        rc = mod.main(["--base", root_commit[0]])
    finally:
        os.chdir(old_cwd)
    assert rc in (mod.RC_OK, mod.RC_VIOLATIONS)
    out = capsys.readouterr().out
    assert "check-meta-guards" in out


def test_splitlines_register_returns_none_when_module_lacks_the_shape(tmp_path) -> None:
    """The file exists but is not the real register -- absence of the shape
    this check needs is `None` (skipped), never a crash or a silent pass."""
    mod = _load()
    dest = (tmp_path / "tests"
            / "test_preset_git_splitlines_register_1130.py")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("X = 1\n", encoding="utf-8")
    _write(tmp_path, "presets/git/sample.py",
           "def f(out):\n    return out.splitlines()\n")
    offenders = mod.check_splitlines_register(tmp_path, ["presets/git/sample.py"])
    assert offenders is None


def test_encoding_seam_returns_none_when_the_scan_module_cannot_load(tmp_path) -> None:
    dest = tmp_path / "tests" / "test_encoding_seam.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("raise RuntimeError('broken guard module')\n", encoding="utf-8")
    mod = _load()
    records = mod.check_encoding_seam(tmp_path, ["presets/git/whatever.py"])
    assert records is None


def test_main_declines_when_the_diff_against_an_explicit_base_fails(
        tmp_path, monkeypatch, capsys) -> None:
    """`--base` naming a ref that does not exist: `_changed_files` fails, and
    that is `could not check`, never a quiet zero-file clean."""
    mod = _load()
    root = _sandboxed_repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=root, timeout=15)
    (root / "committed.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, timeout=15)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-q", "-m", "init"], cwd=root, timeout=15)
    monkeypatch.setattr(mod._ces, "repo_root", lambda _cwd: root)
    old_cwd = Path.cwd()
    try:
        os.chdir(root)
        rc = mod.main(["--base", "does-not-exist-ref"])
    finally:
        os.chdir(old_cwd)
    assert rc == mod.RC_COULD_NOT_CHECK


def test_main_reports_every_family_when_all_three_are_violated(
        tmp_path, monkeypatch, capsys) -> None:
    """One sandbox, one violation per guard family, one `main()` call -- the
    aggregation loop's three `violated`/print branches in the same run,
    rather than three separate single-guard checks."""
    mod = _load()
    root = _sandboxed_repo(tmp_path)
    _with_encoding_seam_module(root)
    subprocess.run(["git", "init", "-q"], cwd=root, timeout=15)
    monkeypatch.setattr(mod._ces, "repo_root", lambda _cwd: root)

    env_offender = _write(root, "tests/test_all_three_env.py",
                           "import subprocess, sys\n"
                           "def f():\n"
                           "    subprocess.run([sys.executable, '-c', 'pass'], "
                           "env={'PATH': ''})\n")
    split_offender = _write(root, "presets/git/all_three_split.py",
                             "def f(out):\n    return out.splitlines()\n")
    seam_offender = _write(root, "presets/git/all_three_seam.py",
                            "import subprocess\n"
                            "def f():\n"
                            "    subprocess.run(['echo', 'hi'], text=True)\n")

    rc = mod.main([str(env_offender), str(split_offender), str(seam_offender)])
    assert rc == mod.RC_VIOLATIONS
    out = capsys.readouterr().out
    assert "env-scrub" in out
    assert "splitlines" in out
    assert "encoding-seam" in out


def test_encoding_seam_reports_a_warning_for_an_undecidable_call(tmp_path) -> None:
    """`**kwargs` at a subprocess spawn is undecidable, not clean -- severity
    `warning`, never folded into `ok`."""
    mod = _load()
    _with_encoding_seam_module(tmp_path)
    _write(tmp_path, "presets/git/undecidable.py",
           "import subprocess\n"
           "def f(opts):\n"
           "    subprocess.run(['echo', 'hi'], **opts)\n")
    records = mod.check_encoding_seam(tmp_path, ["presets/git/undecidable.py"])
    assert records is not None
    assert any(r["severity"] == "warning" for _p, r in records), records


def test_main_prints_encoding_seam_warnings_without_flagging_a_violation(
        tmp_path, monkeypatch, capsys) -> None:
    mod = _load()
    root = _sandboxed_repo(tmp_path)
    _with_encoding_seam_module(root)
    monkeypatch.setattr(mod._ces, "repo_root", lambda _cwd: root)
    scratch = _write(root, "presets/git/undecidable_main.py",
                      "import subprocess\n"
                      "def f(opts):\n"
                      "    subprocess.run(['echo', 'hi'], **opts)\n")
    rc = mod.main([str(scratch)])
    out = capsys.readouterr().out
    assert "calls the scan cannot judge" in out
    assert rc == mod.RC_OK
