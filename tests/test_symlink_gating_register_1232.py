"""#1232 -- every symlink-creating call site in `tests/`, as a build gate.

#1143 gave the suite one probed symlink decision (`tests/_symlink.py`). #1205
fixed the two sites in `test_path_meta_bulk_1126.py` that never asked it, and
its own body called that "two sites". Sweeping the tree with an AST walk rather
than a grep -- a docstring naming `symlink_to` is not a call site -- the class
was **28 uncertified call sites across 18 files**, out of 71 sites in 38 files.
The issue's estimate of ~19 across 13 was low; the count is stated here so the
next reader does not re-derive it.

**What "uncertified" means, and why it is not the same as "ungated".** Most of
the 71 sites never reach a runner without the create-symlink privilege at all,
because the test is already skipped there for an unrelated and correct reason --
a `posix_only` class, an `O_NOFOLLOW` mark, a `try/except OSError` fallback. A
sweep that gated those too would be motion. So this register does not ask "is
there a `require_symlink()` above this line"; it asks whether *some* mechanism
stands between the call and a privilege-less runner, and records which one.

**The label is derived, not asserted.** The `..._is_still_the_one_in_the_code`
test recomputes every label from the AST and compares. That is the difference
between a register and a comment: a site that quietly loses its
`@requires_symlink` goes red while still being listed here, which is the failure
mode a prose table has.

**Measured, not reasoned, about Windows:** GitHub Actions `windows-latest` *has*
the privilege today -- job 93605739570 on run 31434590388 (master, 2026-08-10)
printed `symlink-capability(#1143): available -- 0 symlink-dependent tests
skipped for it`. So none of the 28 were red on this repo's CI. The gate is for
the runner that is not that one: a contributor's Windows without Developer Mode,
a self-hosted leg, or an Actions image that stops granting it. On such a runner
28 sites raised `OSError` where their gated siblings skipped, and four of the
files are the containment suites -- `test_security_safe_path`,
`test_edge_cases_at_file_security`, `test_edge_cases_paste_security`,
`test_security_xml` -- so the leg would have reported the containment tests as
broken code rather than as unrun.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

#: Marks whose presence means the test never runs on a platform that might lack
#: the privilege, for a reason that has nothing to do with symlinks. Matched
#: against the *source text* of the decorator or `pytestmark`, after local names
#: are expanded to their module-level definitions.
PLATFORM_TOKENS = (
    "os.name == 'nt'", 'os.name == "nt"',
    "sys.platform == 'win32'", 'sys.platform == "win32"',
    "platform.system() == 'Windows'",
    "AF_UNIX", "geteuid", "O_NOFOLLOW",
    "posix_only", "needs_dir_fd", "needs_nofollow", "needs_relative_ops",
    "windows_has_no_usable_bash",
)

#: The mechanisms this file recognises. Anything else is `UNGATED`.
A = "A: @requires_symlink on the test (or an enclosing one)"
B = "B: require_symlink() in the body"
D = "D: a local _can_symlink() probe with its own pytest.skip"
E = "E: the call is inside a try/except OSError"
P = "P: the test is platform-skipped anyway, for an unrelated reason"
UNGATED = "UNGATED"


class _Visitor(ast.NodeVisitor):
    """Collect symlink-creating calls in one file, keyed by enclosing def."""

    def __init__(self, rel, assigns, mod_platform, found):
        self._rel = rel
        self._assigns = assigns
        self._mod_platform = mod_platform
        self._found = found
        self._scope = []
        self._trys = []

    def _push(self, node):
        self._scope.append(node)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _push
    visit_AsyncFunctionDef = _push
    visit_ClassDef = _push

    def visit_Try(self, node):
        self._trys.append([
            ast.unparse(h.type) if h.type is not None else "bare"
            for h in node.handlers
        ])
        for stmt in node.body:
            self.visit(stmt)
        self._trys.pop()
        for handler in node.handlers:
            self.visit(handler)
        for stmt in list(node.orelse) + list(node.finalbody):
            self.visit(stmt)

    def _decorator_source(self, node):
        text = " ".join(ast.unparse(d) for d in getattr(node, "decorator_list", []))
        for name, value in self._assigns.items():
            if name in text:
                text += " " + value
        return text

    def _mechanism(self):
        for node in self._scope:
            if "requires_symlink" in self._decorator_source(node):
                return A
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = ast.unparse(node)
                if "require_symlink()" in body:
                    return B
                if "_can_symlink(" in body and "pytest.skip" in body:
                    return D
        for handlers in self._trys:
            if any(h == "bare" or "OSError" in h for h in handlers):
                return E
        if self._mod_platform:
            return P
        for node in self._scope:
            if any(tok in self._decorator_source(node) for tok in PLATFORM_TOKENS):
                return P
        return UNGATED

    def visit_Call(self, node):
        func = node.func
        creates = isinstance(func, ast.Attribute) and func.attr in ("symlink_to", "symlink")
        if creates:
            key = "{0}::{1}".format(
                self._rel, ".".join(n.name for n in self._scope) or "<module>")
            self._found.setdefault(key, []).append((node.lineno, self._mechanism()))
        self.generic_visit(node)


def _call_sites():
    """`tests/file.py::enclosing.def` -> [(lineno, mechanism), ...]."""
    found = {}
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        module = ast.parse(path.read_text(encoding="utf-8"))
        assigns = {}
        for node in module.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigns[target.id] = ast.unparse(node.value)
        mod_platform = any(
            tok in assigns.get("pytestmark", "") for tok in PLATFORM_TOKENS)
        _Visitor(rel, assigns, mod_platform, found).visit(module)
    return found


def _mechanisms():
    """`key` -> the one mechanism guarding every call site under it."""
    out = {}
    for key, sites in _call_sites().items():
        kinds = set(mech for _line, mech in sites)
        out[key] = kinds.pop() if len(kinds) == 1 else "MIXED: " + repr(sorted(kinds))
    return out


#: Every symlink-creating call site in `tests/`, and what stands between it and
#: a runner with no create-symlink privilege. Keyed by `path::enclosing def`,
#: not by line number, so an unrelated edit above does not make it stale.
REGISTER = {
    'tests/_symlink.py::_probe': E,
    'tests/test_append_op_383.py::test_append_through_symlink_writes_to_the_target': B,
    'tests/test_coverage_gate_workdir_877.py::test_refuses_when_the_work_dir_path_is_a_symlink': B,
    'tests/test_edge_cases_at_file_security.py::TestSymlinkedAtFile.test_symlink_followed_and_read': B,
    'tests/test_edge_cases_at_file_security.py::TestSymlinkedAtFile.test_symlink_to_nonexistent_target_errors_cleanly': B,
    'tests/test_edge_cases_at_file_security.py::TestSymlinkedAtFile.test_symlink_to_secret_file_is_read': B,
    'tests/test_edge_cases_glob.py::test_symlink_loop_does_not_infinite_loop': A,
    'tests/test_edge_cases_glob.py::test_symlink_to_dir_outside_cwd_traversed_with_followlinks_false': A,
    'tests/test_edge_cases_glob.py::test_symlink_to_file_outside_cwd_included': A,
    'tests/test_edge_cases_paste_security.py::test_paste_symlink_follows_to_target': B,
    'tests/test_edge_cases_paste_security.py::test_paste_symlink_target_updated_not_replaced': B,
    'tests/test_edge_cases_paste_security.py::test_paste_toctou_tmp_replaced_with_symlink.racing_replace': B,
    'tests/test_edge_cases_replace_lines.py::test_replace_lines_on_symlink': A,
    'tests/test_edge_cases_vim.py::test_symlink_path_edits_target_and_preserves_symlink': B,
    'tests/test_entry_point_shim_931.py::test_entry_point_survives_a_symlink_from_an_unrelated_cwd': B,
    'tests/test_gc_474.py::test_entry_whose_stat_fails_is_kept_and_reported': A,
    'tests/test_git_env_leak_416.py::_build_project': E,
    'tests/test_git_state_guard.py::_guarded_project': E,
    'tests/test_git_timeout_disclosure_650.py::_git_only_path': P,
    'tests/test_mcp_daemon_dirfd_598.py::TestAFailedLogOpenDoesNotOrphanTheServer.test_a_squatted_stderr_symlink_does_not_leave_the_subprocess_running': P,
    'tests/test_mcp_runtime_dir_nofollow_583.py::TestASymlinkedRuntimeDirStillWorks.test_a_symlink_to_a_missing_dir_is_refused_with_a_sentence': P,
    'tests/test_mcp_runtime_dir_nofollow_583.py::linked': B,
    'tests/test_mixed_tree_guard_678.py::test_same_checkout_is_not_a_mix': B,
    'tests/test_oss_train_containment_1246.py::test_a_symlink_inside_the_root_pointing_outside_is_refused': A,
    'tests/test_oss_train_containment_1246.py::test_a_symlink_relinked_mid_run_does_not_redirect_a_later_target.relinking_run': A,
    'tests/test_oss_train_containment_1246.py::test_all_is_not_trusted_either_when_a_discovered_name_is_a_symlink': A,
    'tests/test_oss_train_containment_1246.py::toctou_world': B,
    'tests/test_path_meta_bulk_1126.py::test_a_relative_symlink_is_still_answered_about_its_own_repo': B,
    'tests/test_path_meta_bulk_1126.py::test_a_symlink_gets_the_same_marker_from_either_route': B,
    'tests/test_path_meta_bulk_1126.py::test_a_symlink_is_never_answered_from_another_repo': B,
    'tests/test_pre_push_interpreter_572.py::_Sandbox.__init__': P,
    'tests/test_read.py::test_path_meta_suffix_broken_symlink': B,
    'tests/test_read.py::test_read_meta_symlink': P,
    'tests/test_review_regressions_395.py::test_gate_follows_symlinks_to_the_real_repo': B,
    'tests/test_security_claude_log.py::TestSymlinkInProjectsDir.test_symlink_in_projects_dir_itself': B,
    'tests/test_security_claude_log.py::TestSymlinkInProjectsDir.test_symlink_to_external_jsonl_is_followed': B,
    'tests/test_security_config.py::test_symlinked_config_to_etc_passwd_errors_cleanly': B,
    'tests/test_security_edge_cases.py::test_edit_on_symlink_clobbers_link_with_regular_file': B,
    'tests/test_security_mcp_daemon.py::TestSymlinkAttackOnSocketPath.test_unlink_removes_symlink_not_target': P,
    'tests/test_security_mcp_daemon_148.py::TestSymlinkInvocation.test_runs_clean_through_symlink': P,
    'tests/test_security_safe_path.py::TestSafePathBasics.test_symlink_crossing_rejected': B,
    'tests/test_security_tree_sitter.py::test_map_directory_with_symlink_loop_terminates': B,
    'tests/test_security_tree_sitter.py::test_map_walk_does_not_follow_symlinks': B,
    'tests/test_security_tree_sitter.py::test_tree_symlink_loop_deep_still_bounded': B,
    'tests/test_security_tree_sitter.py::test_tree_symlink_loop_with_depth_cap': B,
    'tests/test_security_xml.py::test_symlink_to_nonxml_file_clean_error': A,
    'tests/test_security_xml.py::test_symlink_to_valid_xml_reads_through': A,
    'tests/test_session_hook_supertool_checkout_711.py::test_the_wrapper_that_is_no_longer_created_would_have_declined': P,
    'tests/test_stat.py::test_stat_broken_symlink': B,
    'tests/test_stat.py::test_stat_symlink': B,
    'tests/test_symlink_capability_1143.py::test_the_probe_agrees_with_the_filesystem': E,
    'tests/test_symlink_rollback_identity_1136.py::_symlink': B,
    'tests/test_validate_header_forgery_and_payload_scope.py::sentinel_tree': B,
    'tests/test_watch_channel_health_hostile_file_1184_1187.py::_can_symlink': E,
    'tests/test_watch_channel_health_hostile_file_1184_1187.py::test_read_health_refuses_a_symlinked_health_file': D,
    'tests/test_watch_channel_health_hostile_file_1184_1187.py::test_the_symlink_refusal_is_its_own_state': D,
    'tests/test_watch_channel_health_hostile_file_1184_1187.py::test_the_symlinked_health_file_never_reaches_the_report': D,
    'tests/test_watch_channel_stranded_hostile_state_1191.py::_can_symlink': E,
    'tests/test_watch_channel_stranded_hostile_state_1191.py::test_a_symlinked_state_file_is_not_parsed': D,
    'tests/test_watch_channel_stranded_hostile_state_1191.py::test_an_unreadable_state_file_is_never_rendered_as_no_watchers': D,
    'tests/test_watch_channel_stranded_hostile_state_1191.py::test_one_hostile_file_does_not_hide_the_other_rows': D,
    'tests/test_watch_channel_stranded_hostile_state_1191.py::test_the_symlink_refusal_is_its_own_state_and_is_reported': D,
    'tests/test_watch_pid_read_hostile_1200.py::_hostile_symlink': B,
    'tests/test_watch_transport_read_state_hostile_1197.py::_can_symlink': E,
    'tests/test_watch_transport_read_state_hostile_1197.py::test_a_dangling_symlink_is_not_reported_as_no_state_file': D,
    'tests/test_watch_transport_read_state_hostile_1197.py::test_a_symlinked_gl_mrs_state_file_is_not_parsed': D,
    'tests/test_watch_transport_read_state_hostile_1197.py::test_a_symlinked_state_file_is_not_parsed': D,
    'tests/test_watch_transport_read_state_hostile_1197.py::test_the_symlink_refusal_is_its_own_state_and_not_could_not_be_read': D,
    'tests/test_watch_transport_read_state_hostile_1197.py::test_the_symlink_refusal_reaches_the_board': D,
}

#: The escape hatch, deliberately empty. A site belongs here only if failing --
#: not skipping -- on a privilege-less runner is the *intended* signal, and the
#: value must say why. Empty is the correct state: no assertion in this tree is
#: about the absence of the privilege itself, and the one place that observes it
#: (`_symlink._probe`) catches its own `OSError`.
UNGATED_BY_DESIGN = {}


def test_no_symlink_call_site_is_left_ungated() -> None:
    """The whole issue: an ungated site fails where its siblings skip."""
    ungated = dict(
        (key, [line for line, mech in sites if mech == UNGATED])
        for key, sites in _call_sites().items()
    )
    ungated = dict(
        (k, v) for k, v in ungated.items() if v and k not in UNGATED_BY_DESIGN)
    assert not ungated, (
        "these symlink-creating call sites reach a runner with no "
        "create-symlink privilege unguarded, where they raise OSError and are "
        "reported as a defect in the code under test (#1205, #1232). Add "
        "`require_symlink()` from `tests/_symlink.py`, or "
        "`@requires_symlink`: " + repr(sorted(ungated.items()))
    )


def test_the_gate_turns_a_failure_into_a_skip_for_real(tmp_path) -> None:
    """A register that only reads the AST could be satisfied by a dead gate.

    Run three of the newly-gated files in a child pytest with the privilege
    forced absent -- the construction `test_symlink_gating_1205.py` uses, and
    the only way to observe the difference from a machine that has it.
    """
    plugin = (
        "import errno" + chr(10)
        + "import os" + chr(10)
        + "import pathlib" + chr(10)
        + "import sys" + chr(10) + chr(10) + chr(10)
        + "def pytest_configure(config):" + chr(10)
        + "    sys.path.insert(0, " + repr(str(TESTS)) + ")" + chr(10)
        + "    import _symlink" + chr(10)
        + "    _symlink._PROBE = (False, 'forced absent by the #1232 register')" + chr(10) + chr(10)
        + "    def _refuse(*a, **k):" + chr(10)
        + "        raise OSError(errno.EPERM, 'symlink refused (#1232 register)')" + chr(10) + chr(10)
        + "    os.symlink = _refuse" + chr(10)
        + "    pathlib.Path.symlink_to = _refuse" + chr(10)
    )
    (tmp_path / "nosymlinkplugin1232.py").write_text(plugin, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(tmp_path),
               PYTEST_ADDOPTS="", PYTEST_DISABLE_PLUGIN_AUTOLOAD="")
    r = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_stat.py", "tests/test_append_op_383.py",
         "tests/test_security_safe_path.py",
         "-k", "symlink", "-rs",
         "-p", "nosymlinkplugin1232", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-p", "no:cov", "-q", "--no-header", "-o", "addopts="],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace", env=env)
    assert r.returncode == 0, (
        "with the privilege absent these gated tests did not all skip:"
        + os.linesep + r.stdout + os.linesep + r.stderr)
    assert " failed" not in r.stdout and " error" not in r.stdout, r.stdout
    assert "4 skipped" in r.stdout, (
        "expected the four symlink-creating tests of those three files to be "
        "collected and skipped -- a run that collected nothing exits 0 too:"
        + os.linesep + r.stdout + os.linesep + r.stderr)
    import _symlink  # noqa: PLC0415
    assert _symlink.TOKEN in r.stdout, (
        "the skips did not carry " + _symlink.TOKEN + ", so they are not "
        "separable from the other ~680 skips in a leg:" + os.linesep + r.stdout)
    assert "4 symlink-dependent tests did NOT run" in r.stdout, (
        "conftest's terminal line under-counted. Before this issue it read `0 "
        "symlink-dependent tests did NOT run` on this very run while four "
        "tests were failing for exactly that reason -- the reporter built to "
        "make the blind spot legible was itself reporting an absence it had "
        "produced. It only counts skips carrying the token, so a gate is what "
        "makes its number true:" + os.linesep + r.stdout)


def test_every_call_site_is_registered() -> None:
    unregistered = sorted(k for k in _mechanisms() if k not in REGISTER)
    assert not unregistered, (
        "new symlink-creating call sites in tests/. Gate them, then record the "
        "mechanism here so the next sweep starts from a true list (#1232): "
        + repr(unregistered))


def test_the_register_names_no_site_that_is_gone() -> None:
    """A register listing sites that no longer exist is not being read."""
    live = _mechanisms()
    stale = sorted(k for k in REGISTER if k not in live)
    assert not stale, "REGISTER entries with no call site left: " + repr(stale)


def test_the_recorded_mechanism_is_still_the_one_in_the_code() -> None:
    """The load-bearing one: the label is re-derived, never trusted.

    A site that keeps its register entry and loses its `@requires_symlink` is
    exactly what a prose table cannot see.
    """
    live = _mechanisms()
    drifted = dict(
        (k, (REGISTER[k], live[k])) for k in REGISTER
        if k in live and REGISTER[k] != live[k])
    assert not drifted, (
        "the mechanism recorded here is no longer the mechanism in the code "
        "(recorded, actual): " + repr(drifted))


def test_every_by_design_exception_states_a_reason() -> None:
    for key, reason in UNGATED_BY_DESIGN.items():
        assert len(reason) > 40, (key, reason)
