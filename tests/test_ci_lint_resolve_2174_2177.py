"""#2174 / #2177 -- `ci_lint_resolve_root.py` is the only `resolve` command in
the tree, and `_validator_resolve` (`_supertool.py`) is not an adapter-style
reader: it takes the first stdout line and trusts it as a resolved path,
without ever checking `returncode`.

#2174: `guard_main` publishes a crash receipt -- one JSON object -- on stdout
and exits 0. That receipt is indistinguishable from a real path to
`_validator_resolve` as it stands today, so a crash becomes "resolved file:
{...json...}", `ci-lint.py` reports "file not found", and a
`rollback_on_fail: true` validator silently reverts a correct edit.

#2177: `_repo_root` returns `None` (and `main()` prints nothing) for three
"I could not look" cases -- git absent, git timed out, not a repo -- and for
the one legitimate "I looked, there is no root config" case. All four render
downstream as the identical `{"skipped": "no target resolved"}`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import supertool  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RESOLVER = REPO / "validators" / "common" / "ci_lint_resolve_root.py"


def _run_resolver(*args, env=None, cwd=None):
    return subprocess.run(
        [sys.executable, str(RESOLVER), *args],
        capture_output=True, text=True, timeout=15,
        encoding="utf-8", errors="replace", env=env, cwd=cwd,
    )


# ---------------------------------------------------------------------------
# #2174
# ---------------------------------------------------------------------------

def test_a_crash_receipt_on_stdout_is_not_read_as_a_resolved_path(tmp_path: Path, monkeypatch) -> None:
    """MUST FIRE: `_validator_resolve` must recognize `guard_main`'s crash
    receipt shape and refuse to hand it back as a path.

    Reproduces a real crash in-process: `main()` resolves a root fine, then
    something after that raises (here, `os.path.isfile` blowing up on the
    candidate path -- the same shape a permissions error or a stat() edge
    case would produce). `guard_main` catches it and prints
    `{"tool": "ci-lint-resolve", "ok": false, ...}` on stdout with exit 0,
    genuinely produced by the module under test, not a mock of its output.
    """
    import importlib.util
    sys.path.insert(0, str(RESOLVER.parent))
    spec_mod = importlib.util.spec_from_file_location("ci_lint_resolve_root_2174", RESOLVER)
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    f = repo / "README.md"
    f.write_text("hi\n")

    def _boom(_path):
        raise RuntimeError("simulated stat failure")
    monkeypatch.setattr(mod.os.path, "isfile", _boom)
    monkeypatch.setattr(sys, "argv", ["resolve_root.py", str(f)])

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.guard_main(mod.TOOL, mod.main)
    out = buf.getvalue().strip()
    assert out, "the crash net should still publish a receipt"
    payload = json.loads(out.splitlines()[-1])
    assert payload.get("ok") is False
    assert payload.get("tool") == "ci-lint-resolve"

    # This is the actual defect surface: `_validator_resolve` runs the
    # resolve command as a real subprocess and reads its first stdout line as
    # a path. Point it at a stub that reproduces exactly this receipt and it
    # must NOT hand the JSON back as a resolved target.
    stub = tmp_path / "crash_stub.py"
    stub.write_text(
        "import sys, json\n"
        "print(json.dumps(" + repr(payload) + "))\n"
    )
    spec = {"resolve": "{python} " + str(stub) + " {file}"}
    resolved = supertool._validator_resolve(spec, str(f))
    assert resolved != out.splitlines()[-1], (
        "the raw JSON crash receipt was handed back verbatim as a path: "
        + repr(resolved))
    assert not (isinstance(resolved, str) and resolved.startswith("{")), (
        "a crash receipt must never be returned as a resolved path: "
        + repr(resolved))


def test_the_crash_net_census_now_catches_this_file() -> None:
    """MUST FIRE: `ci_lint_resolve_root.py` has a `__main__` block and calls
    `guard_main`, so #1697's adapter sweep should not be blind to it just
    because it lives under `validators/common/`."""
    from test_adapter_crash_net_1697 import RESOLVERS  # noqa: E402
    names = [p.name for p in RESOLVERS]
    assert "ci_lint_resolve_root.py" in names, (
        "the census gap named in #2174: a `common/` file with its own "
        "`__main__` block and its own `guard_main` call is invisible to the "
        "adapter sweep. RESOLVERS = " + repr(names))


# ---------------------------------------------------------------------------
# #2177
# ---------------------------------------------------------------------------

def test_git_binary_absent_is_distinguishable_from_no_root_config(tmp_path: Path) -> None:
    f = tmp_path / "README.md"
    f.write_text("hi\n")
    env = {k: v for k, v in os.environ.items() if k != "PATH"}
    env["PATH"] = str(tmp_path / "empty-bin")
    os.makedirs(env["PATH"], exist_ok=True)
    r = _run_resolver(str(f), env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() != "", (
        "git absent must not print the same nothing as 'no root config'")


def test_not_a_repo_is_distinguishable_from_no_root_config(tmp_path: Path) -> None:
    f = tmp_path / "not-a-repo" / "README.md"
    f.parent.mkdir()
    f.write_text("hi\n")
    r = _run_resolver(str(f))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() != "", (
        "not being inside a git repo must not print the same nothing as "
        "'no root config'")


def test_genuine_no_root_config_still_reads_as_nothing_to_check(tmp_path: Path) -> None:
    """MUST NOT FIRE, on the same fixture family: the legitimate "no CI here"
    case must still resolve to the caller's existing skip, not spuriously
    report an error."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    f = repo / "README.md"
    f.write_text("hi\n")
    r = _run_resolver(str(f))
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    spec = {"resolve": "{python} " + str(RESOLVER) + " {file}"}
    resolved = supertool._validator_resolve(spec, str(f))
    assert resolved is None, (
        "a repo with genuinely no root .gitlab-ci.yml must still resolve to "
        "None (the existing skip path), not an error: " + repr((out, resolved)))


# ---------------------------------------------------------------------------
# The through-path: `_validator_resolve`'s RESOLVE-ERROR sentinel must
# actually reach `_validator_run_one`'s final skip dict, not just its own
# return value -- MUST FIRE, because nothing above this drives that far.
# ---------------------------------------------------------------------------

def test_a_resolve_error_reaches_the_final_skip_dict_with_its_reason(tmp_path: Path) -> None:
    repo = tmp_path / "not-a-repo"
    repo.mkdir()
    f = repo / "README.md"
    f.write_text("hi\n")
    spec = {"resolve": "{python} " + str(RESOLVER) + " {file}"}
    result = supertool._validator_run_one("ci-lint", spec, str(f))
    assert result is not None
    assert result.get("tool") == "ci-lint"
    assert "skipped" in result, (
        "a RESOLVE-ERROR sentinel must still render as a skip, not silently "
        "vanish or be read as `ok`: " + repr(result))
    assert "not inside a git repository" in result["skipped"], (
        "the reason from `_repo_root` must survive all the way to the "
        "validator's own skip message, not just `_validator_resolve`'s "
        "return value: " + repr(result))
    assert result["skipped"] != "no target resolved", (
        "a could-not-look reason must not collapse back into the same "
        "sentence as a genuine no-target-to-check: " + repr(result))


def test_a_resolve_command_that_cannot_be_spawned_is_not_read_as_a_quiet_none(
        tmp_path: Path) -> None:
    """The launch-failure gap: `_validator_resolve`'s own subprocess.run can
    fail to even start the resolve command (nonexistent binary in the spec),
    and that must not fold into the same bare-`None` silence #2177 closed one
    call frame down, inside `ci_lint_resolve_root.py` itself."""
    spec = {"resolve": "supertool-nonexistent-binary-2177-xyz {file}"}
    resolved = supertool._validator_resolve(spec, str(tmp_path / "f.txt"))
    assert resolved is not None, (
        "a resolve command that could not even be spawned must not resolve "
        "to bare None -- indistinguishable from 'looked, found nothing'")
    assert resolved.startswith("RESOLVE-ERROR: "), (
        "expected the shared RESOLVE-ERROR protocol: " + repr(resolved))
