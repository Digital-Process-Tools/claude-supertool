"""#2408 -- the generic `@payload`/`args` route #1165 added regressed the
preset ops that already had their OWN named-field payload convention.

`gh-issue-create`, `gh-issue-comment`, `gh-pr-create`, `gh-pr-edit` and
`gl-issue-create` have taken an `@FILE`/`@-` payload since long before
#1165 -- but it is a from-scratch named-field payload (title, body,
labels, ...) their own preset script parses and validates, never a
`:::`-delimited field list in `_AT_FILE_REGISTRY` (their syntax strings
are `op:@FILE | op:@-`, with no field list to derive one from).

#1165 added a generic route that fires on ANY preset op with no
registered named fields, gated only on `_op_is_preset_op` and the
reference resolving -- which could not distinguish these five ops (whose
lack of a registry entry is a pre-existing, deliberate fact) from a
genuine `args`-only op like `gh-job` (the shape #1165 was actually
written for). The result on master (dc431bc9, landed as PR #2403) for
THREE of these five -- `gh-issue-create`, `gh-pr-create` and
`gl-issue-create`, the ones called as bare `op:@FILE`/`op:@-` with no
positional argument ahead of the payload -- was that every real payload
was refused before its own script ever saw it -- reproduced here as the
exact red this file pins:

    printf 'title = "probe"' | python3 supertool.py 'gh-issue-create:@-'
    -> ERROR: @file payload for op 'gh-issue-create' has unknown
       field(s) title -- accepted: args (a list of strings, one per
       positional argument).

The other two -- `gh-issue-comment:ID:@FILE` and `gh-pr-edit:ID:@FILE` --
were NOT actually broken by this regression: their colon shape puts the
issue/PR number at `parts[1]` and the payload reference at `parts[2]`,
and the generic route's own gate only ever inspects `parts[1].startswith
("@")`, so it never fired for them regardless of this bug. Verified
directly (mocked-subprocess dispatch of `gh-pr-edit:1:@-` against
dc431bc9 unmodified) rather than assumed from the issue text, which
named `gh-pr-edit` as a fourth affected op alongside the three above --
that claim does not hold up. Both are still included in this file and
in the fix's exclusion, because `repo_target: "payload"` is the correct
general signal for "this op owns its payload shape" independent of
which colon position the `@` reference happens to land in, and a future
change to either op's calling convention (dropping the leading ID, say)
must not silently regress back into the generic route.

The fix scopes the generic route off any op whose own `repo_target` mode
starts with "payload" -- the codebase's existing signal for "takes its
own named-field payload, parsed by the preset script itself".

Every negative case here ("must not be refused as an args-only op") is
paired with the positive control this repo's own review convention asks
for: `gh-job`, whose `repo_target` is `True` ("op" mode), must still take
the generic `args` route #1165 added it for.
"""
from __future__ import annotations

import json
import shlex

import supertool


class _FakeResult:
    def __init__(self, returncode=0, stdout="ok\n", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capture_subprocess_run(monkeypatch):
    """Stub `subprocess.run` so the real preset script never actually
    executes -- no `gh`/`glab`, no network, no auth. This file is only
    about which ROUTE the dispatcher picks before the subprocess call,
    never about what the script does once invoked. Recording argv also
    lets a test confirm the payload reference reached the child
    UNCHANGED (the literal `@-`), rather than having been pre-expanded
    into `args`-shaped flags by the generic route.
    """
    calls = []

    def fake(argv, **kwargs):
        calls.append(list(argv))
        return _FakeResult()

    monkeypatch.setattr(supertool.subprocess, "run", fake)
    return calls


_NAMED_FIELD_PAYLOAD_OPS = [
    "gh-issue-create",
    "gh-issue-comment",
    "gh-pr-create",
    "gh-pr-edit",
    "gl-issue-create",
]

_DISPATCH_CALLS = {
    "gh-issue-create": "gh-issue-create:@-",
    "gh-issue-comment": "gh-issue-comment:1:@-",
    "gh-pr-create": "gh-pr-create:@-",
    "gh-pr-edit": "gh-pr-edit:1:@-",
    "gl-issue-create": "gl-issue-create:@-",
}


class TestNamedFieldPayloadOpsAreNotRoutedGeneric:
    """Every op in #2408's own `repo_target: "payload"` family: a real
    payload reaches the preset script's own parser rather than being
    refused as an `args`-only call.
    """

    def test_repo_target_modes_marks_all_five_as_payload(self):
        modes = supertool._repo_target_modes()
        for op in _NAMED_FIELD_PAYLOAD_OPS:
            assert modes.get(op, "").startswith("payload"), (op, modes.get(op))

    def test_gh_issue_create_reaches_its_own_script_unrefused(
        self, monkeypatch, with_preset_op,
    ):
        with_preset_op("gh-issue-create")
        calls = _capture_subprocess_run(monkeypatch)
        out = supertool.dispatch("gh-issue-create:@-")
        assert "unknown field(s)" not in out
        assert calls, "expected the real preset script to be invoked"
        # The `@-` reference is handed to the child LITERALLY -- it is the
        # script's own job to read stdin and parse title/body/labels/...,
        # not the generic dispatcher's.
        assert calls[0][-1] == "@-"

    def test_gh_issue_comment_reaches_its_own_script_unrefused(
        self, monkeypatch, with_preset_op,
    ):
        with_preset_op("gh-issue-comment")
        calls = _capture_subprocess_run(monkeypatch)
        out = supertool.dispatch("gh-issue-comment:1:@-")
        assert "unknown field(s)" not in out
        assert calls
        assert calls[0][-1] == "@-"

    def test_gh_pr_create_reaches_its_own_script_unrefused(
        self, monkeypatch, with_preset_op,
    ):
        with_preset_op("gh-pr-create")
        calls = _capture_subprocess_run(monkeypatch)
        out = supertool.dispatch("gh-pr-create:@-")
        assert "unknown field(s)" not in out
        assert calls
        assert calls[0][-1] == "@-"

    def test_gh_pr_edit_reaches_its_own_script_unrefused(
        self, monkeypatch, with_preset_op,
    ):
        with_preset_op("gh-pr-edit")
        calls = _capture_subprocess_run(monkeypatch)
        out = supertool.dispatch("gh-pr-edit:1:@-")
        assert "unknown field(s)" not in out
        assert calls
        assert calls[0][-1] == "@-"

    def test_gl_issue_create_reaches_its_own_script_unrefused(
        self, monkeypatch,
    ):
        # `with_preset_op` reads from THIS repo's own declared presets
        # (`.supertool.json` here enables `github`/`slack`/`git`, not
        # `gitlab`), so it cannot install `gl-issue-create` -- the op
        # genuinely is not loaded in this repo's own config, which is a
        # true and separate fact from the routing bug this file is about.
        # Installed directly off the shipped `presets/gitlab.json` entry
        # instead, the same way `preset_op_route_state` in conftest.py
        # builds one entry's registry state without going through
        # `_load_config()` at all.
        with open("presets/gitlab.json", encoding="utf-8") as f:
            gitlab_ops = json.load(f)["ops"]
        entry = gitlab_ops["gl-issue-create"]
        assert entry.get("repo_target", "").startswith("payload")
        monkeypatch.setattr(supertool, "_CONFIG", {"ops": {"gl-issue-create": entry}})
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY_BUILT", False)
        supertool._build_at_file_registry()
        calls = _capture_subprocess_run(monkeypatch)
        out = supertool.dispatch("gl-issue-create:@-")
        assert "unknown field(s)" not in out
        assert calls
        assert calls[0][-1] == "@-"


class TestArgsOnlyPresetOpsStillTakeTheGenericRoute:
    """The positive control (`gh-job`, `repo_target: True`) -- the class
    #1165 was actually written for must keep working after this fix
    scopes the route away from the payload-shaped ops above.
    """

    def test_gh_job_is_not_marked_payload_mode(self):
        modes = supertool._repo_target_modes()
        assert modes.get("gh-job") == "op"

    def test_synthetic_args_only_op_still_takes_generic_route(
        self, tmp_path, monkeypatch,
    ):
        """Same synthetic-op harness #1165's own regression file uses
        (`_register_argv_echo`) -- an op with no `repo_target` at all
        (like the ones #1165's tests register) must still be routed
        through the generic `args` payload path.
        """
        script = tmp_path / "argv.py"
        script.write_text(
            "import sys\n"
            "for i, a in enumerate(sys.argv[1:], 1):\n"
            "    print(str(i) + '=' + a)\n"
        )
        script_arg = shlex.quote(script.as_posix())
        monkeypatch.setattr(supertool, "_CONFIG", {
            "ops": {
                "say2408": {
                    "cmd": f"{{python}} {script_arg} {{args}}",
                    "safety": "read-only",
                }
            }
        })
        spec = tmp_path / "p.json"
        spec.write_text(json.dumps({"args": ["pattern:with:colons:"]}))
        out = supertool.dispatch(f"say2408:@{spec}")
        assert "1=pattern:with:colons:" in out
