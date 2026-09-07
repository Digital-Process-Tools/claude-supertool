"""#1165 — preset ops gain a generic @payload route (`op:@file` / `op:@-`),
so a colon-bearing argument the colon CLI cannot express has an escape.

Core ops have had `@payload` since #625 (`grep:@-`, `edit:@file`). Preset
ops took `{args}` only, substituted from parts the colon tokenizer already
split on ':' — the one character that survives no colon-CLI spelling at all
(everything else, `$;&`` `*` spaces/quotes/backslashes, already round-trips
through `shlex.quote`). `gh-job:ID:grep:PATTERN` rejoins everything after the
mode (#1145) and covers the ordinary case, but a pattern that must genuinely
END in ':', or one whose intended reading disagrees with the rejoin
heuristic, had no explicit form to fall back to.

This closes the gap GENERICALLY (design question 1 in the issue): any
preset op declared under `.supertool.json`'s "ops" section that has no
already-registered ':::' named-field route (`edit`, `git-commit`, ...)
accepts `op:@file` / `op:@-` with a payload carrying one key, `args` — a
list of strings, each becoming one positional argument verbatim. No colon
split, no rejoin heuristic, no per-op opt-in: the rejoin stays the ergonomic
default for the colon CLI (design question 3), and `args` is only reached
when a caller explicitly writes `op:@...`.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool


def _register_argv_echo(name: str = "say", *, tmp_path: Path) -> None:
    """A synthetic preset op whose only job is to print each argv entry it
    receives, one per line, as `N=value` — so a test can see exactly what
    `{args}` substitution produced without any real preset's own logic
    (network calls, git, etc.) getting in the way.
    """
    script = tmp_path / "argv.py"
    script.write_text(
        "import sys\n"
        "for i, a in enumerate(sys.argv[1:], 1):\n"
        "    print(str(i) + '=' + a)\n"
    )
    supertool._CONFIG = {
        "ops": {
            name: {"cmd": f"{{python}} {script} {{args}}", "safety": "read-only"}
        }
    }


class TestColonCliHardWall:
    """The gap this issue is filed about, pinned so a future change to the
    tokenizer is measured against the same repro.
    """

    def test_colon_bearing_argument_is_split_by_the_tokenizer(
        self, tmp_path: Path
    ) -> None:
        _register_argv_echo(tmp_path=tmp_path)
        out = supertool.dispatch("say:pattern:with:colons")
        # The one value the caller meant ("pattern:with:colons") never
        # reaches the op as a single argument on the colon CLI — it is
        # split into three.
        assert "1=pattern:with:colons" not in out
        assert "1=pattern" in out
        assert "3=colons" in out


class TestGenericPresetPayloadRoute:
    """The escape hatch: `op:@file` / `op:@-` with `{args = [...]}`."""

    def test_colon_bearing_argument_survives_the_payload_route(
        self, tmp_path: Path
    ) -> None:
        _register_argv_echo(tmp_path=tmp_path)
        spec = tmp_path / "p.json"
        spec.write_text(json.dumps({"args": ["pattern:with:colons:"]}))
        out = supertool.dispatch(f"say:@{spec}")
        assert "1=pattern:with:colons:" in out

    def test_multiple_args_stay_positional_and_separate(
        self, tmp_path: Path
    ) -> None:
        _register_argv_echo(tmp_path=tmp_path)
        spec = tmp_path / "p.json"
        spec.write_text(json.dumps({"args": ["first:one", "second"]}))
        out = supertool.dispatch(f"say:@{spec}")
        assert "1=first:one" in out
        assert "2=second" in out

    def test_stdin_at_dash_route_works_too(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import io
        _register_argv_echo(tmp_path=tmp_path)
        monkeypatch.setattr(
            "sys.stdin", io.StringIO('args = ["a:b:c"]\n'))
        out = supertool.dispatch("say:@-")
        assert "1=a:b:c" in out

    def test_missing_args_field_is_refused_with_a_remedy(
        self, tmp_path: Path
    ) -> None:
        _register_argv_echo(tmp_path=tmp_path)
        spec = tmp_path / "p.json"
        spec.write_text(json.dumps({}))
        out = supertool.dispatch(f"say:@{spec}")
        assert "ERROR" in out
        assert "args" in out

    def test_unknown_field_is_refused_not_dropped(self, tmp_path: Path) -> None:
        _register_argv_echo(tmp_path=tmp_path)
        spec = tmp_path / "p.json"
        spec.write_text(json.dumps({"args": ["x"], "typo": "y"}))
        out = supertool.dispatch(f"say:@{spec}")
        assert "ERROR" in out
        assert "typo" in out

    def test_wrong_type_for_args_is_refused(self, tmp_path: Path) -> None:
        _register_argv_echo(tmp_path=tmp_path)
        spec = tmp_path / "p.json"
        spec.write_text(json.dumps({"args": 5}))
        out = supertool.dispatch(f"say:@{spec}")
        assert "ERROR" in out

    def test_single_string_args_value_is_accepted(self, tmp_path: Path) -> None:
        """`args` may be a bare string as well as a list — the singular
        spelling every other payload route (`tools`, `path`) already
        accepts for a one-item case.
        """
        _register_argv_echo(tmp_path=tmp_path)
        spec = tmp_path / "p.json"
        spec.write_text(json.dumps({"args": "solo:value"}))
        out = supertool.dispatch(f"say:@{spec}")
        assert "1=solo:value" in out


class TestGenericRouteScope:
    """The generic route is scoped to preset ops declared under "ops" —
    never a builtin that legitimately has no named-field route.
    """

    def test_builtin_with_no_registered_route_is_unaffected(
        self, tmp_path: Path
    ) -> None:
        """Auditor review note: pair the negative claim with a positive
        control in the SAME fixture, so a broken harness (nothing at all
        happening) cannot pass this test the same way a correctly-scoped
        route does. `say` -- registered by the identical
        `_register_argv_echo` helper every positive test in this file
        uses -- proves the payload route is live in this process; `wc`
        proves it is not reached for a builtin with no registered route.
        """
        _register_argv_echo(tmp_path=tmp_path)
        control_spec = tmp_path / "control.json"
        control_spec.write_text(json.dumps({"args": ["x"]}))
        control_out = supertool.dispatch(f"say:@{control_spec}")
        assert "1=x" in control_out

        spec = tmp_path / "p.json"
        spec.write_text(json.dumps({"args": ["x"]}))
        # `wc` (word count) has no ':::' syntax and is not in
        # _READ_OP_AT_FIELDS either — it must fall through and try to open
        # the literal path "@<spec>", not be intercepted as a payload
        # reference.
        out = supertool.dispatch(f"wc:@{spec}")
        assert "field(s)" not in out
        assert "reads its argv from" not in out

    def test_op_is_preset_op_helper(self, tmp_path: Path) -> None:
        _register_argv_echo(tmp_path=tmp_path)
        assert supertool._op_is_preset_op("say") is True
        assert supertool._op_is_preset_op("wc") is False
        assert supertool._op_is_preset_op("does-not-exist") is False

    def test_literal_at_prefixed_first_argument_still_passes_through(
        self, tmp_path: Path
    ) -> None:
        """Self-review finding: a preset op's own colon syntax can legitimately
        expect a literal first argument that starts with '@' (a mention, an
        npm-scope name, ...). Gated the same way the read-op @payload route
        already is -- on the reference actually resolving, not on the bare
        '@' prefix -- so this must keep working exactly as it did before
        preset ops had a payload route at all: no interception, no reference
        to a payload route in the output.
        """
        _register_argv_echo(tmp_path=tmp_path)
        out = supertool.dispatch("say:@octocat")
        assert "1=@octocat" in out
        assert "ERROR" not in out
        assert "reads its argv from" not in out

    def test_literal_at_prefixed_argument_with_more_tokens_passes_through(
        self, tmp_path: Path
    ) -> None:
        _register_argv_echo(tmp_path=tmp_path)
        out = supertool.dispatch("say:@octocat:issue-123")
        assert "1=@octocat" in out
        assert "2=issue-123" in out
        assert "ERROR" not in out
