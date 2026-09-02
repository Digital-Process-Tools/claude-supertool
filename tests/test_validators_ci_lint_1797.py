"""#1797 -- a ci-lint validator: `glab ci lint` against the resolved GitLab
CI root config, with the two traps the issue names covered by their own
tests.

Trap 1: linting an included file directly is wrong -- an include is not a
standalone document. Trap 2: a network/auth failure must not read as an
invalid config, because `rollback_on_fail: true` would then silently revert
a genuinely correct edit during an outage.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from _adapter_budget import adapter_budget
from _adapter_verdict import assert_declined, assert_ok

ADAPTER = Path(__file__).parent.parent / "validators" / "ci-lint" / "ci-lint.py"
RESOLVER = Path(__file__).parent.parent / "validators" / "common" / "ci_lint_resolve_root.py"


def _python_stub(tmp_path: Path, name: str, body: str) -> str:
    stub = tmp_path / f"{name}.py"
    stub.write_text(body)
    return f"{shlex.quote(sys.executable)} {shlex.quote(stub.as_posix())}"


def _run(f: Path, env: dict) -> dict:
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(ADAPTER), env=env,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


def test_no_arg_returns_schema_error() -> None:
    r = subprocess.run([sys.executable, str(ADAPTER)], capture_output=True,
                        text=True, timeout=adapter_budget(ADAPTER), encoding="utf-8", errors="replace")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ci-lint"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


def test_missing_binary_is_skipped_not_ok(tmp_path: Path) -> None:
    """The third state, per #1797's own scope note: `glab`/`gh` are already
    assumed present by the gh-*/gl-* op families, but an absent binary here
    must still report `skipped`, never `ok`."""
    f = tmp_path / ".gitlab-ci.yml"
    f.write_text("stages:\n  - build\n")
    env = {**os.environ, "GLAB_BIN": "glab-that-does-not-exist-xyz"}
    data = _run(f, env)
    assert data["tool"] == "ci-lint"
    assert "skipped" in data
    assert "ok" not in data
    assert "not found" in data["skipped"]


def test_valid_config_via_stub_is_ok(tmp_path: Path) -> None:
    f = tmp_path / ".gitlab-ci.yml"
    f.write_text("stages:\n  - build\n")
    bin_cmd = _python_stub(
        tmp_path, "stub_valid",
        "import sys; print('CI/CD YAML is valid!'); sys.exit(0)\n",
    )
    env = {**os.environ, "GLAB_BIN": bin_cmd}
    data = _run(f, env)
    assert data["tool"] == "ci-lint"
    assert_ok(data)


def test_invalid_config_via_stub_is_not_ok_and_never_skipped(tmp_path: Path) -> None:
    """Trap 1's own reproduction, condensed: a real invalid config -- glab's
    own literal wording, confirmed against its shipped string table -- must
    roll back, and must never be reported as a mere decline."""
    f = tmp_path / ".gitlab-ci.yml"
    f.write_text("stages:\n  - build\njob1:\n  stage: nosuchstage\n"
                  "  script:\n    - echo hi\n")
    body = (
        "import sys\n"
        "sys.stderr.write('.gitlab-ci.yml is invalid.\\n"
        "1 job1 job: chosen stage nosuchstage does not exist;\\n"
        "  available stages are .pre, build, test, deploy, .post\\n')\n"
        "sys.exit(1)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_invalid", body)
    env = {**os.environ, "GLAB_BIN": bin_cmd}
    data = _run(f, env)
    assert data["tool"] == "ci-lint"
    assert "skipped" not in data
    assert_declined(data)
    assert data["count"] == 1
    assert "does not exist" in data["errors"][0]["msg"]


def test_repo_resolution_failure_is_skipped_never_invalid(tmp_path: Path) -> None:
    """Trap 2's own reproduction: `glab` declining to identify a GitLab
    project/host (no remote, no auth) must never read as 'invalid config' --
    that would roll back a correct edit during exactly the outage the issue
    warns about."""
    f = tmp_path / ".gitlab-ci.yml"
    f.write_text("stages:\n  - build\n")
    body = (
        "import sys\n"
        "sys.stderr.write('You must be in a GitLab project repository for "
        "this action: not a git repository\\n')\n"
        "sys.exit(1)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_norepo", body)
    env = {**os.environ, "GLAB_BIN": bin_cmd}
    data = _run(f, env)
    assert data["tool"] == "ci-lint"
    assert "skipped" in data
    assert "ok" not in data
    assert "could not confirm" in data["skipped"]


def test_http_auth_failure_is_skipped_never_invalid(tmp_path: Path) -> None:
    """A resolvable project but a rejected request (401/403 from the lint
    endpoint itself, no `glab auth login` prompt this time) is the same
    third state -- glab reached GitLab and still could not answer."""
    f = tmp_path / ".gitlab-ci.yml"
    f.write_text("stages:\n  - build\n")
    body = (
        "import sys\n"
        "sys.stderr.write('Post https://gitlab.com/api/v4/projects/1/ci/"
        "lint: 403 {message: 403 Forbidden}.\\n')\n"
        "sys.exit(1)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_forbidden", body)
    env = {**os.environ, "GLAB_BIN": bin_cmd}
    data = _run(f, env)
    assert data["tool"] == "ci-lint"
    assert "skipped" in data
    assert "ok" not in data


def test_timeout_is_skipped_never_invalid(tmp_path: Path, monkeypatch) -> None:
    """A hung network call is Trap 2 too: glab never answered, so this must
    not read as a verdict about the file."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("ci_lint_mod", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["glab"], timeout=30)

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    monkeypatch.setattr(mod.shutil, "which", lambda _b: "/usr/bin/glab")

    f = tmp_path / ".gitlab-ci.yml"
    f.write_text("stages:\n  - build\n")
    monkeypatch.setattr(sys, "argv", ["ci-lint.py", str(f)])

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main()
    data = json.loads(buf.getvalue().strip())
    assert "skipped" in data
    assert "ok" not in data
    assert "timed out" in data["skipped"]


def test_resolver_maps_an_include_to_the_repo_root_config(tmp_path: Path) -> None:
    """Trap 1's resolve half: an included file under `.gitlab/ci/` must
    resolve to the root `.gitlab-ci.yml`, not to itself."""
    repo = tmp_path / "repo"
    (repo / ".gitlab" / "ci").mkdir(parents=True)
    root_cfg = repo / ".gitlab-ci.yml"
    root_cfg.write_text("include:\n  - local: .gitlab/ci/build.yml\n")
    include = repo / ".gitlab" / "ci" / "build.yml"
    include.write_text("build:\n  script:\n    - echo hi\n")

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    r = subprocess.run(
        [sys.executable, str(RESOLVER), str(include)],
        capture_output=True, text=True, timeout=adapter_budget(RESOLVER),
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    resolved = r.stdout.strip()
    assert resolved and os.path.samefile(resolved, root_cfg)


def test_resolver_prints_nothing_when_no_root_config_exists(tmp_path: Path) -> None:
    """No `.gitlab-ci.yml` at the repo root -- the caller (`_validator_resolve`)
    reads empty stdout as 'skip this validator', which is correct: there is
    nothing here for ci-lint to check."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    some_file = repo / "README.md"
    some_file.write_text("hi\n")

    r = subprocess.run(
        [sys.executable, str(RESOLVER), str(some_file)],
        capture_output=True, text=True, timeout=adapter_budget(RESOLVER),
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_resolver_no_arg_prints_nothing() -> None:
    r = subprocess.run([sys.executable, str(RESOLVER)], capture_output=True,
                        text=True, timeout=adapter_budget(RESOLVER), encoding="utf-8", errors="replace")
    assert r.returncode == 0
    assert r.stdout.strip() == ""

def test_invalid_marker_is_tied_to_the_files_own_name_not_a_bare_substring(tmp_path: Path) -> None:
    """An auth/token message that happens to contain the words 'is invalid.'
    on its own must still be `skipped`, never misread as this file failing
    real GitLab validation -- the collision an unqualified substring check
    would create, and exactly what Trap 2 warns rollback_on_fail against."""
    f = tmp_path / ".gitlab-ci.yml"
    f.write_text("stages:\n  - build\n")
    body = (
        "import sys\n"
        "sys.stderr.write('your personal access token is invalid.\\n')\n"
        "sys.exit(1)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_token_invalid", body)
    env = {**os.environ, "GLAB_BIN": bin_cmd}
    data = _run(f, env)
    assert data["tool"] == "ci-lint"
    assert "skipped" in data
    assert "ok" not in data


def test_count_contract_is_declared_on_every_verdict_bearing_emit(tmp_path: Path) -> None:
    """`COUNT_CONTRACT` is spliced into every emit call that carries `count`,
    not just declared as a module-level constant nothing reads (SCHEMA.md:
    declaring it is mandatory for a shipped adapter)."""
    f = tmp_path / ".gitlab-ci.yml"
    f.write_text("stages:\n  - build\n")

    bin_cmd = _python_stub(
        tmp_path, "stub_valid",
        "import sys; print('CI/CD YAML is valid!'); sys.exit(0)\n",
    )
    env = {**os.environ, "GLAB_BIN": bin_cmd}
    data = _run(f, env)
    assert data["count_basis"] == "measured"
    assert data["errors_truncated"] is False

    body = (
        "import sys\n"
        "sys.stderr.write('.gitlab-ci.yml is invalid.\\n1 job: bad stage\\n')\n"
        "sys.exit(1)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_invalid2", body)
    env = {**os.environ, "GLAB_BIN": bin_cmd}
    data = _run(f, env)
    assert data["count_basis"] == "measured"
    assert data["errors_truncated"] is False



def _load_ci_lint_module():
    """Import ci-lint.py in-process (hyphenated filename) so a test can
    monkeypatch `subprocess.run` and observe the exact argv the adapter
    builds, without needing a real executable at a fabricated path -- a
    real cross-platform binary at a spaced path is not something a test can
    portably fabricate (#2176).
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("ci_lint_adapter_2176", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_glab_bin_program_files_style_path_is_not_split_at_the_space(
    tmp_path: Path, monkeypatch,
) -> None:
    """#2176 -- a Windows-Program-Files-shaped GLAB_BIN, actually installed
    there (a real file with the execute bit set), must be used AS ONE PATH.
    Before the fix, POSIX-mode `shlex.split` split the unquoted path at the
    space in "Program Files", and the adapter ran `["<tmp>/Program", "ci",
    "lint", ...]` -- a binary that does not exist, reported as GLAB_BIN not
    found, even though the real one was sitting right there.
    """
    bin_dir = tmp_path / "Program Files" / "glab"
    bin_dir.mkdir(parents=True)
    real_glab = bin_dir / "glab.exe"
    real_glab.write_text("")
    real_glab.chmod(0o755)

    f = tmp_path / ".gitlab-ci.yml"
    f.write_text("stages:\n  - build\n")

    monkeypatch.setenv("GLAB_BIN", real_glab.as_posix())
    monkeypatch.setattr(sys, "argv", ["ci-lint.py", str(f)])

    mod = _load_ci_lint_module()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        import subprocess as _sp
        return _sp.CompletedProcess(cmd, 0, stdout="CI/CD YAML is valid!\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    captured_emit = []
    monkeypatch.setattr(mod, "emit", lambda obj: captured_emit.append(obj))

    mod.main()

    assert captured["cmd"][0] == real_glab.as_posix(), captured["cmd"]
    assert captured_emit[0]["ok"] is True, captured_emit[0]
