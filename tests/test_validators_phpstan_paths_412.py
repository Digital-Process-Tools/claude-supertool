"""#412 — PHPSTAN_MCP_PATHS: don't pay 9.2s to be told the file is out of scope.

The adapter cannot read phpstan's `--paths`: that scope lives in the daemon's
own configuration and the adapter has no parser for it. So the knowledge has to
arrive from outside, and the two failure directions are not symmetric:

- **Skipping a file that IS in scope** — silent loss of analysis. The file looks
  handled and is not. Unacceptable.
- **Analysing a file that is NOT in scope** — 9.2s wasted, correct result.
  Merely slow.

Which is why this is opt-in and never inferred. Unset, the adapter must behave
byte-identically to before this feature existed — the daemon stays the only
authority on scope. Set, it is the repo making an explicit statement.

These tests pin the three claims the issue names, and each fails loudly if the
implementation does nothing: the daemon is genuinely NOT contacted for an
out-of-scope path (contacting it raises), an in-scope file still reaches it,
and an unset var reaches it for any path at all.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_ADAPTER = Path(__file__).parent.parent / "validators" / "phpstan-mcp" / "phpstan-mcp.py"
_REFUSAL = Path(__file__).parent.parent / "validators" / "common" / "refusal.py"

PATHS_ENV = "PHPSTAN_MCP_PATHS"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _adapter():
    return _load(_ADAPTER, "phpstan_mcp_adapter_412")


def _refusal_mod():
    return _load(_REFUSAL, "refusal_412")


def _mcp_clean() -> dict:
    return {"jsonrpc": "2.0", "id": 2,
            "result": {"structuredContent": {"errors": [], "exit_code": 0}}}


def _run_main(monkeypatch: pytest.MonkeyPatch, target: Path,
              *, contacted: list) -> dict:
    """Drive `main` with a daemon that RECORDS contact instead of doing work.

    The point of the recorder is that a no-op implementation cannot pass: an
    out-of-scope run that still dials the daemon leaves a mark here.
    """
    mod = _adapter()

    def _ensure(cwd):
        contacted.append(("ensure_daemon", cwd))
        return "/sock"

    def _call(sock, fpath):
        contacted.append(("ndjson_call", fpath))
        return _mcp_clean()

    monkeypatch.setattr(mod, "ensure_daemon", _ensure)
    monkeypatch.setattr(mod, "ndjson_call", _call)
    captured: list = []
    monkeypatch.setattr("builtins.print", lambda s: captured.append(s))
    assert mod.main(["phpstan-mcp.py", str(target)]) == 0
    return json.loads(captured[-1])


def _run_main_exploding(monkeypatch: pytest.MonkeyPatch, target: Path) -> dict:
    """Same, but any contact with the daemon is a hard failure.

    `main` swallows exceptions into an `adapter` error result, so a raise does
    not propagate — it shows up as a receipt with code `adapter`, which the
    assertions below reject just as loudly.
    """
    mod = _adapter()

    def _boom(*a, **k):
        raise AssertionError("daemon was contacted for an out-of-scope path")

    monkeypatch.setattr(mod, "ensure_daemon", _boom)
    monkeypatch.setattr(mod, "ndjson_call", _boom)
    captured: list = []
    monkeypatch.setattr("builtins.print", lambda s: captured.append(s))
    assert mod.main(["phpstan-mcp.py", str(target)]) == 0
    return json.loads(captured[-1])


def _php(tmp_path: Path, *parts: str) -> Path:
    f = tmp_path.joinpath(*parts)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("<?php\n")
    return f


# ---------------------------------------------------------------------------
# The three claims
# ---------------------------------------------------------------------------

def test_out_of_scope_path_never_opens_the_socket(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The whole point: no daemon round trip for a file we know is out of scope."""
    monkeypatch.setenv(PATHS_ENV, str(tmp_path / "src"))
    target = _php(tmp_path, "tests", "FooTest.php")
    data = _run_main_exploding(monkeypatch, target)
    assert data.get("skipped"), f"expected a skip, got {data!r}"
    assert not any(e.get("code") == "adapter" for e in data.get("errors") or []), (
        "the adapter reached the daemon and turned the refusal into an error: "
        + json.dumps(data))
    # #515: no verdict keys at all — the refusal reports nothing about the file
    # rather than reporting nothing wrong with it.
    assert "count" not in data and "errors" not in data


def test_in_scope_path_still_reaches_the_daemon(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The unacceptable direction, pinned: an in-scope file is still analysed."""
    monkeypatch.setenv(PATHS_ENV, str(tmp_path / "src"))
    target = _php(tmp_path, "src", "Foo.php")
    contacted: list = []
    data = _run_main(monkeypatch, target, contacted=contacted)
    assert "skipped" not in data, f"an in-scope file was silently skipped: {data!r}"
    assert [c[0] for c in contacted] == ["ensure_daemon", "ndjson_call"]
    assert data["ok"] is True


def test_unset_var_behaves_exactly_as_before(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unset = no local knowledge = the daemon decides, for ANY path."""
    monkeypatch.delenv(PATHS_ENV, raising=False)
    target = _php(tmp_path, "somewhere", "else", "Foo.php")
    contacted: list = []
    data = _run_main(monkeypatch, target, contacted=contacted)
    assert "skipped" not in data
    assert [c[0] for c in contacted] == ["ensure_daemon", "ndjson_call"]


def test_blank_var_is_treated_as_unset(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An empty or whitespace value is not an empty allowlist that skips
    everything — that would turn a stray `export PHPSTAN_MCP_PATHS=` into a
    repo-wide silent loss of analysis."""
    monkeypatch.setenv(PATHS_ENV, "   ")
    target = _php(tmp_path, "anything", "Foo.php")
    contacted: list = []
    data = _run_main(monkeypatch, target, contacted=contacted)
    assert "skipped" not in data
    assert contacted


# ---------------------------------------------------------------------------
# The skip reason points at the config that caused it
# ---------------------------------------------------------------------------

def test_skip_reason_names_the_env_var_not_phpstan(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A wrong skip is a supertool misconfiguration. The reason must say so —
    blaming `--paths` sends the reader to a tool that never saw the file."""
    monkeypatch.setenv(PATHS_ENV, str(tmp_path / "src"))
    target = _php(tmp_path, "tests", "FooTest.php")
    data = _run_main_exploding(monkeypatch, target)
    reason = data["skipped"]
    assert PATHS_ENV in reason, reason
    assert "--paths" not in reason, reason
    assert "phpstan" not in reason.replace(PATHS_ENV, "").lower(), reason


def test_skip_is_reported_through_the_shared_third_state(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Same receipt shape as every other skip, so the core's no-rollback,
    no-delta, no-cache handling applies without a second code path."""
    monkeypatch.setenv(PATHS_ENV, str(tmp_path / "src"))
    target = _php(tmp_path, "tests", "FooTest.php")
    data = _run_main_exploding(monkeypatch, target)
    assert data["tool"] == "phpstan-mcp"
    # #515: the shared shape is the OMITTING one — `ok: true` on a receipt that
    # means "never looked at" is the pass this state exists not to be.
    assert not ({"ok", "count", "errors"} & set(data)), data
    assert isinstance(data["duration_ms"], int)


# ---------------------------------------------------------------------------
# Root matching: the boundary is a separator, not a prefix
# ---------------------------------------------------------------------------

def test_sibling_directory_sharing_a_prefix_is_outside(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`/srcbad/Foo.php` is not inside `/src`. A bare `startswith` says it is."""
    monkeypatch.setenv(PATHS_ENV, str(tmp_path / "src"))
    target = _php(tmp_path, "srcbad", "Foo.php")
    data = _run_main_exploding(monkeypatch, target)
    assert data.get("skipped")


def test_prefix_sibling_the_other_way_round_is_in_scope(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """And the mirror: a root that is a prefix of nothing still matches itself."""
    monkeypatch.setenv(PATHS_ENV, str(tmp_path / "srcbad"))
    target = _php(tmp_path, "srcbad", "Foo.php")
    contacted: list = []
    data = _run_main(monkeypatch, target, contacted=contacted)
    assert "skipped" not in data
    assert contacted


def test_trailing_separator_on_a_root_is_harmless(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(PATHS_ENV, str(tmp_path / "src") + os.sep)
    target = _php(tmp_path, "src", "Foo.php")
    contacted: list = []
    data = _run_main(monkeypatch, target, contacted=contacted)
    assert "skipped" not in data
    assert contacted


def test_relative_root_resolves_against_the_working_directory(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`.supertool.json` entries are committed and shared — a root written as
    `src` must mean this checkout's `src`, like every other path in the file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(PATHS_ENV, "src")
    inside = _php(tmp_path, "src", "Foo.php")
    outside = _php(tmp_path, "tests", "FooTest.php")
    contacted: list = []
    assert "skipped" not in _run_main(monkeypatch, inside, contacted=contacted)
    assert contacted
    assert _run_main_exploding(monkeypatch, outside).get("skipped")


# ---------------------------------------------------------------------------
# The helper itself — generic by construction, so phpmd-mcp can reuse it
# ---------------------------------------------------------------------------

def test_helper_takes_the_env_var_name_so_a_second_adapter_can_reuse_it(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The helper must not hardcode PHPSTAN_MCP_PATHS. No second env var is
    shipped until a second adapter asks for one — but the seam is here."""
    ref = _refusal_mod()
    monkeypatch.setenv("SOME_OTHER_MCP_PATHS", str(tmp_path / "src"))
    outside = str(tmp_path / "tests" / "FooTest.php")
    inside = str(tmp_path / "src" / "Foo.php")
    reason = ref.outside_roots(outside, "SOME_OTHER_MCP_PATHS")
    assert reason and "SOME_OTHER_MCP_PATHS" in reason
    assert ref.outside_roots(inside, "SOME_OTHER_MCP_PATHS") is None


def test_helper_accepts_both_pathsep_and_comma_separated_roots(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ref = _refusal_mod()
    a, b = tmp_path / "src", tmp_path / "lib"
    for sep in (os.pathsep, ","):
        monkeypatch.setenv(PATHS_ENV, f"{a}{sep}{b}")
        assert ref.outside_roots(str(a / "Foo.php"), PATHS_ENV) is None
        assert ref.outside_roots(str(b / "Bar.php"), PATHS_ENV) is None
        assert ref.outside_roots(str(tmp_path / "t" / "T.php"), PATHS_ENV)


def test_helper_ignores_empty_entries_between_separators(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A stray separator must not become a root — an empty string abspath()s to
    the working directory and would quietly widen the allowlist to everything."""
    ref = _refusal_mod()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(PATHS_ENV, f"{tmp_path / 'src'}{os.pathsep}{os.pathsep},")
    assert ref.outside_roots(str(tmp_path / "tests" / "T.php"), PATHS_ENV)


def test_helper_matches_the_root_directory_itself(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ref = _refusal_mod()
    root = tmp_path / "src"
    monkeypatch.setenv(PATHS_ENV, str(root))
    assert ref.outside_roots(str(root), PATHS_ENV) is None


def test_helper_returns_none_when_the_var_is_absent(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ref = _refusal_mod()
    monkeypatch.delenv(PATHS_ENV, raising=False)
    assert ref.outside_roots(str(tmp_path / "anything.php"), PATHS_ENV) is None
