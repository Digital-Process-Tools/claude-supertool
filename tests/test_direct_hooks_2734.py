"""#2734 -- the release build's hooks run Python straight from hooks.json.

The Anthropic directory validator holds COMMAND_SCRIPT_NOT_FOLLOWED on a hook
shell script that runs a further file (probe h: session-start.sh running
supertool.py held; probe j: hooks.json running the Python directly cleared).
Claude Code has no per-platform hook command, so the release hooks.json
carries a literal interpreter ladder in shell form -- python3.14 down to
python3.9, then `py -3`, each rung guarded by a literal identity check, never
a bare `python3` (#572, #1382) and never a computed command word (the
validator's UNPINNED_NPX block) -- and runs two new entry points,
hooks/session_start.py and hooks/pre_bash_guard_hook.py.

The source plugin keeps its .sh hooks. These tests pin that the two routes
answer alike on the same inputs, that the ladder falls through to `py -3`, and
that a host with no Python 3 gets one line and exit 0.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "scripts" / "build_release_tree.py"
RELEASE_HOOKS = REPO / "hooks" / "hooks.release.json"
SH = shutil.which("sh")
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    os.name == "nt" or SH is None or BASH is None,
    reason="runs the hook commands through sh and the .sh hooks through bash")

VERSIONED = [f"python3.{m}" for m in range(14, 8, -1)]


def _release_command(event: str) -> str:
    doc = json.loads(RELEASE_HOOKS.read_text(encoding="utf-8"))
    return doc["hooks"][event][0]["hooks"][0]["command"]


def _env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUPERTOOL_")}
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env.update(extra)
    return env


def _run(argv, cwd, stdin="", env=None):
    return subprocess.run(argv, cwd=str(cwd), input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env or _env(), timeout=120)


def _has_versioned_python() -> bool:
    return any(shutil.which(name) for name in VERSIONED)


# -- the build ----------------------------------------------------------------


def _build(tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location("build_release_tree_dh", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "built"
    mod.build(REPO, "HEAD", out, mod.load_config(REPO / ".github" / "release-branch.json"))
    return out


def test_the_built_hooks_json_runs_python_directly(tmp_path) -> None:
    built = _build(tmp_path)
    text = (built / "hooks" / "hooks.json").read_text(encoding="utf-8")
    assert ".sh" not in text, text
    assert "hooks/session_start.py" in text
    assert "hooks/pre_bash_guard_hook.py" in text
    # Never the bare name python3 as a COMMAND (#572): only version-named
    # rungs and py -3. The fallback sentence names it in prose, which is fine.
    for event in ("SessionStart", "PreToolUse"):
        cmd = json.loads(text)["hooks"][event][0]["hooks"][0]["command"]
        assert not re.search(r"(?:^|[;&|]|\b(?:then|if|elif|else))\s+python3\s", cmd), cmd
    assert re.search(r"(?:^|[;&|]|\b(?:then|if|elif|else))\s+python3\s",
                     "if x; then python3 y; fi")              # positive control
    for gone in ("hooks/session-start.sh", "hooks/pre-bash-guard.sh",
                 "hooks/python-ladder.sh", "hooks/hooks.release.json"):
        assert not (built / gone).exists(), gone
    assert (built / "hooks" / "session_start.py").is_file()
    assert (built / "hooks" / "pre_bash_guard_hook.py").is_file()
    assert (built / "hooks" / "pre_bash_guard.py").is_file()


def test_every_rung_is_a_literal_command_word() -> None:
    """A computed command word (`"$PY" x.py`) is the validator's UNPINNED_NPX
    block. Every rung must start with a literal interpreter name."""
    for event in ("SessionStart", "PreToolUse"):
        cmd = _release_command(event)
        assert "$PY" not in cmd and "VIRTUAL_ENV" not in cmd
        rungs = re.findall(r"(\S+(?: -3)?) \"\$\{CLAUDE_PLUGIN_ROOT\}", cmd)
        assert rungs == VERSIONED + ["py -3"], rungs


def test_the_release_hooks_pass_this_repository_s_own_hook_check() -> None:
    """check_release_tree.py encodes the directory's hook-command rules: no
    `python -c`, no variable but CLAUDE_PLUGIN_ROOT, every path written from
    it (so no `/dev/null` redirect). A probe per rung would break both."""
    spec = importlib.util.spec_from_file_location(
        "check_release_tree_dh", REPO / ".github" / "scripts" / "check_release_tree.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree_dh"] = mod  # its @dataclass looks itself up
    spec.loader.exec_module(mod)
    for event in ("SessionStart", "PreToolUse"):
        assert mod.hook_command_problems(_release_command(event)) == [], event
    assert mod.hook_command_problems("python3.12 -c x </dev/null")  # positive control


# -- same answers as the .sh hooks ------------------------------------------


@pytest.mark.skipif(not _has_versioned_python(), reason="no python3.N on PATH")
@pytest.mark.parametrize("command", [
    "git status", "ls -la", "cat README.md", "git commit -m x", "echo hi", "",
])
def test_guard_answers_like_the_shell_wrapper(command) -> None:
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    old = _run([BASH, str(REPO / "hooks" / "pre-bash-guard.sh")], REPO, event)
    new = _run([SH, "-c", _release_command("PreToolUse")], REPO, event)
    assert (new.returncode, new.stdout) == (old.returncode, old.stdout)
    if command in ("git status", "git commit -m x"):
        assert '"permissionDecision":"deny"' in new.stdout   # positive control


@pytest.mark.skipif(not _has_versioned_python(), reason="no python3.N on PATH")
def test_guard_answers_alike_on_a_powershell_event() -> None:
    event = json.dumps({"tool_name": "PowerShell", "tool_input": {"command": "gh pr list"}})
    old = _run([BASH, str(REPO / "hooks" / "pre-bash-guard.sh")], REPO, event)
    new = _run([SH, "-c", _release_command("PreToolUse")], REPO, event)
    assert (new.returncode, new.stdout) == (old.returncode, old.stdout)


def _session_both(tmp_path: Path, prepare=None):
    outs = []
    for name in ("old", "new"):
        proj = tmp_path / name
        proj.mkdir()
        if prepare:
            prepare(proj)
        if name == "old":
            r = _run([BASH, str(REPO / "hooks" / "session-start.sh")], proj)
        else:
            r = _run([SH, "-c", _release_command("SessionStart")], proj)
        listing = sorted(p.name for p in proj.iterdir())
        link = os.readlink(proj / "supertool") if (proj / "supertool").is_symlink() else None
        outs.append((r.returncode, r.stdout.replace(str(proj), "PROJ"), listing, link))
    return outs


@pytest.mark.skipif(not _has_versioned_python(), reason="no python3.N on PATH")
def test_session_start_in_an_empty_project(tmp_path) -> None:
    old, new = _session_both(tmp_path)
    assert new == old
    assert new[3] == str(REPO) + "/supertool.py"          # the wrapper was made


@pytest.mark.skipif(not _has_versioned_python(), reason="no python3.N on PATH")
def test_session_start_leaves_a_stranger_file_alone(tmp_path) -> None:
    old, new = _session_both(
        tmp_path, lambda p: (p / "supertool").write_text("mine\n", encoding="utf-8"))
    assert new == old
    assert "leaving it untouched" in new[1]


@pytest.mark.skipif(not _has_versioned_python(), reason="no python3.N on PATH")
def test_session_start_refuses_inside_a_supertool_checkout(tmp_path) -> None:
    def prepare(p: Path) -> None:
        (p / ".supertool.json").write_text("{}\n", encoding="utf-8")
        (p / "supertool.py").write_text("# a different tree\n", encoding="utf-8")
    old, new = _session_both(tmp_path, prepare)
    assert new == old
    assert "No ./supertool wrapper created here" in new[1]


def test_session_start_relinks_its_own_stale_symlink(tmp_path, monkeypatch) -> None:
    """The plugin cache layout: <cache>/supertool/<version>/supertool.py."""
    sys.path.insert(0, str(REPO / "hooks"))
    import session_start
    cache = tmp_path / "cache" / "supertool"
    for v in ("0.1.0", "0.2.0"):
        (cache / v).mkdir(parents=True)
        (cache / v / "supertool.py").write_text("", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    os.symlink(str(cache / "0.1.0" / "supertool.py"), proj / "supertool")
    monkeypatch.chdir(proj)
    lines = []
    monkeypatch.setattr(session_start, "_say", lines.append)
    session_start.wrapper(str(cache / "0.2.0" / "supertool.py"))
    assert os.readlink("supertool") == str(cache / "0.2.0" / "supertool.py")
    assert lines and "own 0.1.0" in lines[0] and "now 0.2.0; repointed" in lines[0]


# -- the ladder -------------------------------------------------------------


def _py_shim(bindir: Path, marker: Path) -> None:
    """A Windows-launcher stand-in: `py -3 ARGS` runs the real interpreter."""
    shim = bindir / "py"
    shim.write_text(
        "#!/bin/sh\n"
        f"echo called >> '{marker}'\n"
        '[ "$1" = "-3" ] && shift\n'
        f"exec '{sys.executable}' \"$@\"\n", encoding="utf-8")
    shim.chmod(0o755)


def test_the_ladder_falls_through_to_py_3(tmp_path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "py-called"
    _py_shim(bindir, marker)
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    r = _run([SH, "-c", _release_command("PreToolUse")], REPO, event,
             env=_env(PATH=str(bindir)))
    assert r.returncode == 0, r.stderr
    assert '"permissionDecision":"deny"' in r.stdout, r.stdout
    assert marker.read_text(encoding="utf-8").count("called") == 1


def test_no_python_at_all_prints_one_line_and_exits_0(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _run([SH, "-c", _release_command("SessionStart")], tmp_path,
             env=_env(PATH=str(empty)))
    assert r.returncode == 0
    assert r.stdout.count("\n") == 1 and "no Python 3 was found" in r.stdout, r.stdout
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    g = _run([SH, "-c", _release_command("PreToolUse")], tmp_path, event,
             env=_env(PATH=str(empty)))
    assert g.returncode == 0
    doc = json.loads(g.stdout)
    assert "guard did not run" in doc["hookSpecificOutput"]["additionalContext"]


def _build_module():
    spec = importlib.util.spec_from_file_location("build_release_tree_dh2", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_swap_release_hooks_is_a_noop_without_the_key() -> None:
    mod = _build_module()
    contents = {"hooks/hooks.json": b"{}"}
    assert mod.swap_release_hooks(contents, {}) == []
    assert contents == {"hooks/hooks.json": b"{}"}


def test_swap_release_hooks_refuses_a_path_missing_from_the_tree() -> None:
    """A config naming a .sh that is not there must not ship half-swapped."""
    mod = _build_module()
    contents = {"hooks/hooks.json": b"old", "hooks/hooks.release.json": b"new"}
    cfg = {"release_hooks": {"hooks_json": "hooks/hooks.release.json",
                             "drop": ["hooks/session-start.sh"]}}
    with pytest.raises(mod.BuildError):
        mod.swap_release_hooks(contents, cfg)
    contents["hooks/session-start.sh"] = b"#!/bin/bash"
    assert mod.swap_release_hooks(contents, cfg) == [
        "hooks/hooks.release.json", "hooks/session-start.sh"]
    assert contents == {"hooks/hooks.json": b"new"}
