"""Issue #614 — the dispatcher's third state: unavailable here, not unknown.

`gl-mr`, `gh-job` and friends are shipped preset ops. Standing outside a
project that enables the preset, the dispatcher used to answer
`unknown operation: gl-mr` plus a `Valid operations:` list that omitted them.
That message states the op does not exist; the truth is it exists and is
unavailable from this cwd. Same defect class as `docs/validators.md`'s
"Declining instead of guessing" — three states, not two.

Covered here:
  * available        — an enabled op still runs
  * unknown          — a genuine typo still reads as a typo, with no hedging
  * unavailable here — a shipped-preset op names the preset, the cwd, and the fix
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import supertool


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _no_config(monkeypatch: pytest.MonkeyPatch, cwd: Path) -> None:
    """Simulate standing in a directory with no .supertool.json anywhere above."""
    monkeypatch.chdir(cwd)
    supertool._CONFIG = {}
    supertool._CONFIG_CHECKED = True
    supertool._CONFIG_PATH = None


def _probe_cmd(sentinel: str, token: str) -> str:
    """A custom-op cmd that proves it really executed.

    Two things this has to get right, both learned the hard way on this file.

    **Quoting.** Custom ops run argv-form (``shell=False``) through
    ``shlex.split(posix=True)``, which strips *unescaped* quotes of any kind.
    ``{python} -c print('X')`` therefore reaches Python as ``print(X)`` — a
    NameError, not a print. Wrapping the whole payload in double quotes is what
    the rest of the suite already does (``test_op_format``, ``test_notifiers``):
    shlex keeps quotes of the *other* type inside a quoted region, so the inner
    single quotes survive.

    **A token in the output is not proof of execution.** ``print('MR-OK')``
    mis-quoted raises ``NameError: name 'MR' is not defined`` — and on Python
    >= 3.13, whose tracebacks echo the offending ``-c`` source line back, the
    string ``MR-OK`` appears inside that traceback. The assertion then matched
    its own malformed input, reflected out of an error message. It passed
    locally on 3.14 and failed on all five CI legs (3.9-3.12), which is the only
    reason it was caught. So the probe also touches a sentinel file: a
    filesystem side effect that no error message can fake.
    """
    return (
        '{python} -c "'
        f"open('{sentinel}', 'w').close(); print('{token}')"
        '"'
    )


def _assert_probe_ran(out: str, cwd: Path, sentinel: str, token: str) -> None:
    """Assert the op ran to completion, not merely that its token appears."""
    assert "Traceback" not in out, f"the probe itself failed:\\n{out}"
    assert "FAIL" not in out, f"the probe itself failed:\\n{out}"
    assert token in out
    assert (cwd / sentinel).exists(), (
        "the token was in the output but the process never ran — the assertion "
        "is matching an error message again"
    )


def _config_at(monkeypatch: pytest.MonkeyPatch, cwd: Path, cfg: dict) -> Path:
    """Simulate a found .supertool.json at `cwd` holding `cfg`."""
    monkeypatch.chdir(cwd)
    path = cwd / ".supertool.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    supertool._CONFIG = cfg
    supertool._CONFIG_CHECKED = True
    supertool._CONFIG_PATH = str(path)
    return path


# --------------------------------------------------------------------------
# state 1 — available
# --------------------------------------------------------------------------

class TestAvailable:
    def test_enabled_custom_op_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_at(monkeypatch, tmp_path, {
            "ops": {"gl-mr": {"cmd": _probe_cmd("ran-mr.txt", "MR-OK")}},
        })
        out = supertool.dispatch("gl-mr:1")
        _assert_probe_ran(out, tmp_path, "ran-mr.txt", "MR-OK")
        assert "unknown operation" not in out
        assert "unavailable here" not in out

    def test_builtin_op_still_runs_outside_a_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
        _no_config(monkeypatch, tmp_path)
        out = supertool.dispatch("wc:f.txt")
        assert "unknown operation" not in out
        assert "3" in out


# --------------------------------------------------------------------------
# state 2 — genuinely unknown (must NOT be softened into a hedge)
# --------------------------------------------------------------------------

class TestStillUnknown:
    def test_typo_outside_a_project_still_reads_as_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_config(monkeypatch, tmp_path)
        out = supertool.dispatch("raed:foo.py")
        assert "ERROR: unknown operation: raed" in out
        # The whole point of #614's fourth constraint: a typo must not be told
        # it might need a project root.
        assert "unavailable here" not in out
        assert "preset" not in out.lower()

    def test_typo_inside_a_project_still_reads_as_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_at(monkeypatch, tmp_path, {"presets": ["gitlab"], "ops": {}})
        out = supertool.dispatch("raed:foo.py")
        assert "ERROR: unknown operation: raed" in out
        assert "unavailable here" not in out

    def test_valid_operations_list_is_not_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The list printed with 'unknown operation' must not omit real ops.

        It was hand-maintained and had rotted to 18 names while the dispatcher
        accepted far more — the same "absence the tool produced" one layer in.
        """
        _no_config(monkeypatch, tmp_path)
        out = supertool.dispatch("raed:foo.py")
        listed = out.split("Valid operations:", 1)[1]
        for name in ("edit", "vim", "batch", "between", "cwd", "append", "paste"):
            assert name in listed, f"{name} missing from the Valid operations list"

    def test_every_listed_op_is_actually_dispatchable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard against the list rotting in the other direction.

        Probed through the read-only set only: dispatching every advertised name
        would fire mutating and git-touching ops for a listing check. `cwd` is
        excluded because main() honours and strips it before dispatch() is ever
        reached — a valid CLI op that this entry point cannot see.
        """
        _no_config(monkeypatch, tmp_path)
        out = supertool.dispatch("raed:foo.py")
        listed = {n.strip() for n in
                  out.split("Valid operations:", 1)[1].split("\n", 1)[0].split(",")}
        assert supertool._MAIN_LEVEL_OPS <= listed
        probes = (listed & supertool._PARALLEL_SAFE_OPS) - supertool._MAIN_LEVEL_OPS
        assert len(probes) > 10, "probe set collapsed — the test stopped testing"
        for name in sorted(probes):
            probe = supertool.dispatch(f"{name}:")
            assert "unknown operation" not in probe, (
                f"'{name}' is advertised but the dispatcher rejects it"
            )


# --------------------------------------------------------------------------
# state 3 — unavailable from here
# --------------------------------------------------------------------------

class TestUnavailableHere:
    def test_preset_op_outside_a_project_is_not_called_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact regression from #614."""
        _no_config(monkeypatch, tmp_path)
        out = supertool.dispatch("gl-mr:33323:status")
        assert "unknown operation: gl-mr" not in out

    def test_preset_op_outside_a_project_names_preset_cwd_and_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_config(monkeypatch, tmp_path)
        out = supertool.dispatch("gl-mr:33323:status")
        assert "unavailable here" in out
        assert "gitlab" in out, "the message must name the preset that provides it"
        assert str(tmp_path) in out or os.path.realpath(str(tmp_path)) in out, (
            "the message must name the cwd it is complaining about"
        )
        assert "cwd:" in out, "the message must name the documented escape hatch"

    def test_gh_job_the_op_that_cost_the_evening(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_config(monkeypatch, tmp_path)
        out = supertool.dispatch("gh-job:123:fail")
        assert "unknown operation: gh-job" not in out
        assert "github" in out

    def test_near_miss_project_reads_differently_from_no_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config that exists but does not enable the preset is a different
        problem from no config at all, and must produce a different fix."""
        cfg_path = _config_at(monkeypatch, tmp_path, {"presets": ["git"], "ops": {}})
        out = supertool.dispatch("gl-mr:1")
        assert "unavailable here" in out
        assert "gitlab" in out
        assert str(cfg_path) in out, "name the config file that fails to enable it"
        assert '"presets"' in out, "tell them which key to edit"

    def test_no_config_message_does_not_talk_about_editing_a_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_config(monkeypatch, tmp_path)
        out = supertool.dispatch("gl-mr:1")
        assert "no .supertool.json" in out.lower()

    def test_project_op_shadowing_a_preset_name_is_available_not_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project defining its own `gl-mr` must run it, not be told it is
        unavailable — the check is a fallthrough, never a pre-emption."""
        _config_at(monkeypatch, tmp_path, {
            "ops": {"gh-job": {"cmd": _probe_cmd("ran-local.txt", "LOCAL")}},
        })
        out = supertool.dispatch("gh-job:1")
        _assert_probe_ran(out, tmp_path, "ran-local.txt", "LOCAL")
        assert "unavailable here" not in out


# --------------------------------------------------------------------------
# the same absence, in `ops`
# --------------------------------------------------------------------------

class TestOpsDisclosure:
    def test_ops_outside_a_project_discloses_the_hidden_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`ops` from a non-project directory read as the tool's full capability.
        It must say what is not loaded, without listing it."""
        _no_config(monkeypatch, tmp_path)
        out = supertool.op_ops()
        assert "preset" in out.lower()
        assert "gitlab" in out
        assert "cwd:" in out

    def test_ops_disclosure_is_one_line_not_a_second_listing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_config(monkeypatch, tmp_path)
        out = supertool.op_ops()
        footer = [ln for ln in out.splitlines() if "preset" in ln.lower()]
        assert footer, "expected a disclosure line"
        assert len(footer) == 1, (
            f"disclosure bloated to {len(footer)} lines: {footer}. This test "
            "is named for the property that it is one line and not a second "
            "listing; the bound was `<= 2`, which admitted exactly the second "
            "line the name rules out (#731).")
        # No op name from a not-enabled preset may be enumerated.
        assert "gl-issue" not in out and "gh-pr" not in out

    def test_disclosure_leads_outside_a_project_and_trails_inside_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Placement is the judgment, and it is not cosmetic.

        Outside a project the listing misleads, so the line goes above the
        SessionStart truncation point. Inside one the gap is that project's own
        choice, and a permanent banner on the most-read output is just noise.
        """
        _no_config(monkeypatch, tmp_path)
        out = supertool.op_ops()
        body = out.split("\\n\\n")
        assert any("not loaded here" in seg for seg in body[:3]), (
            "outside a project the disclosure must lead"
        )

        _config_at(monkeypatch, tmp_path, {
            "presets": ["git"],
            "builtin-ops": {"read": {"syntax": "read:PATH"}},
        })
        out = supertool.op_ops()
        assert "not loaded here" in out
        assert "not loaded here" not in out.split("## Operations")[0], (
            "inside a project the disclosure must trail, not banner"
        )

    def test_ops_with_every_preset_enabled_has_no_disclosure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        names = sorted(set(supertool._shipped_preset_ops().values()))
        _config_at(monkeypatch, tmp_path, {
            "presets": names,
            "ops": {op: {"cmd": "true"} for op in supertool._shipped_preset_ops()},
            "builtin-ops": {"read": {"syntax": "read:PATH"}},
        })
        out = supertool.op_ops()
        assert "not loaded here" not in out

    def test_ops_compact_disclosure_survives_the_hook_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SessionStart listing truncates from the tail — a disclosure that
        only ever lands at the very bottom is a disclosure nobody reads."""
        _no_config(monkeypatch, tmp_path)
        out = supertool.op_ops(compact=True)
        head = out.encode("utf-8")[: supertool._HOOK_OUTPUT_CAP_BYTES].decode(
            "utf-8", "ignore"
        )
        assert "preset" in head.lower()


# --------------------------------------------------------------------------
# the knowledge source
# --------------------------------------------------------------------------

class TestShippedPresetIndex:
    def test_index_maps_known_ops_to_their_preset(self) -> None:
        idx = supertool._shipped_preset_ops()
        assert idx.get("gl-mr") == "gitlab"
        assert idx.get("gh-job") == "github"
        assert idx.get("git-commit") == "git"
        assert idx.get("radar") == "watch"

    def test_index_names_a_builtin_a_preset_documents(self) -> None:
        """Inverted by #2025, deliberately.

        This used to assert the index held no built-in name at all, enforced by
        a `_BUILTIN_OPS` filter in `_shipped_preset_ops`. That filter stood in
        for "a preset must not shadow a built-in" — which `dispatch` already
        guarantees, by reaching `_resolve_custom_op` only after every built-in
        branch has declined.

        Once `presets/lsp.json` became where those five ops are documented, the
        old assertion made the hint lie in the one direction this whole file
        exists to prevent: `hover` would have been reported as an op that does
        not exist, in a binary that dispatches it.
        """
        idx = supertool._shipped_preset_ops()
        assert idx.get("hover") == "lsp"
        assert idx.get("rename") == "lsp"
